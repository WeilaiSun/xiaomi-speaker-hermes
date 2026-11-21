# Xiaomi Speaker x Hermes —— 小米智能音箱语音接入 Hermes（开源版）
# 基于 Open-XiaoAI 生态构建：流式工具进度播报 / silero VAD / 播放打断 / 多轮对话 / 原生唤醒共存
# License: MIT
#!/usr/bin/env python3
"""
Hermes × 小爱音箱 (OH2P) 语音入口 Server v4 — 整句捕获模式
原理：音箱音频流电平很低(峰值~0.008)，KWS 模型需要峰值归一到 0.7 才能命中
- IDLE: VAD 捕获整句 → 峰值归一化 → 双语 KWS 检测 "Hey Hermes" → 唤醒
- 原生 ASR 事件中的 Hermes 变体作为第二唤醒通道
- LISTENING: VAD 捕获整句 → 归一化 → ASR 识别 → Hermes 执行 → 原生 TTS 播报
"""
import asyncio
import json
import os
import re
import subprocess
import threading
import time
import traceback
import wave

import numpy as np
import sherpa_onnx
import open_xiaoai_server

MODELS = os.environ.get("HERMES_VOICE_MODELS", os.path.expanduser(
    "~/.hermes/services/xiaomi-speaker-hermes/models"
))
SR = 16000
GAIN = 60.0                 # 归一化最大增益
TARGET_PEAK = 0.7           # KWS 命中所需电平
SILENCE_SEC = 0.5           # 静音多久判定句子结束
MIN_UTT_SEC = 0.6          # 最短有效语句
MAX_UTT_SEC = 15.0
# Hermes API Server（常驻进程，消除冷启动；比 hermes -z 快 2-3 倍）
VOICE_API_URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions")
VOICE_LED = 1               # led show 的 L 值（呼吸效果）
WAKE_PATTERNS = ("hermes", "赫米斯", "赫尔墨斯", "黑尔墨斯", "hermans", "小赫")


def load_api_key() -> str:
    env = os.environ.get("HERMES_API_KEY")
    if env:
        return env.strip()
    try:
        for line in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


API_KEY = load_api_key()

# 工具 → 播报动词（会拼上 label 具体内容）
TOOL_VERB = {
    "terminal": "正在执行",
    "read_file": "正在读取",
    "write_file": "正在写",
    "search_files": "正在查找",
    "web_search": "正在搜索",
    "web_extract": "正在阅读网页",
    "vision_analyze": "正在看图",
    "browser_exec": "正在操作浏览器",
    "memory": "正在查记忆",
    "session_search": "正在翻聊天记录",
    "hindsight_recall": "正在查记忆",
    "hindsight_retain": "正在记笔记",
    "todo_list": "正在看任务清单",
    "delegate_task": "正在分派子任务",
}
HEARTBEAT_PHRASES = ["还在处理中", "我再确认一下", "马上就好"]
# ASR 文本唤醒匹配（应对 "Hey Hermes" 被转写成 "嘿 HOMES"/"嘿喝米" 等）
WAKE_RE = re.compile(
    r"(?:herm[a-z]*|homes?|holmes|homers|helms|comerce|commerce|comers|chemist|joker\s*mi"
    r"|赫米斯|赫尔墨斯|和米斯|黑米|喝米|赫米|克米斯|科米斯|莫斯"
    r"|(?:转|找|叫|召唤|接|呼叫)\s*(?:横麦|和卖|和米|莫斯|米斯|赫米|黑米|喝米|克米|科米|herm|home|hel))",
    re.I,
)

WAKE_CN = ("赫米斯", "赫尔墨斯", "嘿喝米", "黑喝米", "赫米", "转hermes", "找hermes", "召唤hermes")


def is_wake_text(text: str) -> bool:
    if not text:
        return False
    if WAKE_RE.search(text):
        return True
    if any(p in text for p in WAKE_CN):
        return True
    # 拼音声母模糊兜底：ASR 把 "转Hermes" 听成什么汉字都无法预测，
    # 但声母序列形状稳定（hermes -> hms / zhuán+h+m / xiǎng+h+m ...）
    try:
        from pypinyin import lazy_pinyin, Style
        ini = "".join(lazy_pinyin(text, style=Style.INITIALS, strict=False))
        ini = re.sub(r"[^a-z]", "", ini.lower())
        for pat in ("zhm", "xhm", "hms", "hrm", "rms", "kms"):
            if pat in ini:
                return True
    except Exception:
        pass
    return False

class SileroVAD:
    """silero 神经网络人声判定（v5 onnx：input/state/sr -> output/stateN）"""

    def __init__(self, model_path: str, threshold: float = 0.40):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.sr = np.array(16000, dtype=np.int64)
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.threshold = threshold
        self.tail = np.zeros(0, dtype=np.float32)

    def prob(self, x: np.ndarray) -> float:
        """x: float32 单声道；返回这批音频里的最大语音概率"""
        buf = np.concatenate([self.tail, x.astype(np.float32)])
        best = 0.0
        while buf.size >= 512:
            chunk = buf[:512]
            buf = buf[512:]
            peak = float(np.max(np.abs(chunk))) or 1e-6
            # 远场语音电平太低，放大到 silero 熟悉的范围（强限幅防止纯噪声被放大误判）
            gain = min(40.0, 0.35 / max(peak, 1e-4))
            out, st = self.sess.run(
                ["output", "stateN"],
                {"input": (chunk * gain).reshape(1, -1), "state": self.state, "sr": self.sr},
            )
            self.state = st
            best = max(best, float(out[0, 0]))
        self.tail = buf
        return best


print("⏳ 加载模型...", flush=True)

kws = sherpa_onnx.KeywordSpotter(
    provider="cpu", num_threads=2, max_active_paths=4,
    keywords_score=2.0, keywords_threshold=0.15, num_trailing_blanks=1,
    keywords_file=f"{MODELS}/keywords.txt",
    tokens=f"{MODELS}/tokens.txt",
    encoder=f"{MODELS}/encoder.onnx",
    decoder=f"{MODELS}/decoder.onnx",
    joiner=f"{MODELS}/joiner.onnx",
)

asr = sherpa_onnx.OnlineRecognizer.from_transducer(
    tokens=f"{MODELS}/tokens.txt",
    encoder=f"{MODELS}/encoder.onnx",
    decoder=f"{MODELS}/decoder.onnx",
    joiner=f"{MODELS}/joiner.onnx",
    num_threads=2, sample_rate=SR, feature_dim=80,
    decoding_method="greedy_search",
)

print("✅ 模型加载完成", flush=True)
IDLE, LISTENING, THINKING, SPEAKING = range(4)


def peak_norm(x: np.ndarray) -> np.ndarray:
    m = float(np.max(np.abs(x))) if len(x) else 0.0
    if m < 1e-6:
        return x
    if m >= TARGET_PEAK:
        return x
    g = min(TARGET_PEAK / m, GAIN)
    return np.clip(x * g, -1.0, 1.0)


def kws_detect(x: np.ndarray):
    """对整句音频跑 KWS，返回命中的关键词或 None"""
    st = kws.create_stream()
    for i in range(0, len(x), 1440):
        st.accept_waveform(SR, x[i:i + 1440])
        while kws.is_ready(st):
            kws.decode_stream(st)
        r = kws.get_result(st)
        if r:
            return r
    return None


def asr_transcribe(x: np.ndarray) -> str:
    st = asr.create_stream()
    st.accept_waveform(SR, x)
    while asr.is_ready(st):
        asr.decode_stream(st)
    return (asr.get_result(st) or "").strip()


class Bridge:
    def __init__(self):
        self.state = IDLE
        self.loop = None
        self.lock = threading.Lock()
        self.chunks = 0
        self.last_dbg = time.time()
        self.led_mode = "off"      # off / listen / breath
        self.led_last_l = 0        # 最后一次 show 的 L（关闭时要用它）
        self.music_playing = False # 原生播放状态（放歌时跳过歌词识别）
        self.history = []          # 多轮对话上下文（最近几轮）
        self.echo_guard_until = 0.0  # 回声抑制截止时间
        self.last_rms = 0.0
        # 整句捕获
        self.buf = []
        self.buf_start = 0.0
        self.last_voice = 0.0
        self.recording = False
        # silero VAD + 播放中打断
        self.silero = None
        try:
            self.silero = SileroVAD(f"{MODELS}/silero_vad.onnx")
        except Exception:
            traceback.print_exc()
        self.playing = False        # 此刻是否在放音（TTS）
        self.play_start = 0.0       # 起播时间
        self.bar_echo = 0.0         # 回声电平跟踪（EMA）
        self.bar_hits = 0.0         # 超阈累计时长
        self.playback_killed = False

    # ---------------- 音频流 ----------------
    def on_input(self, data: bytes):
        with self.lock:
            try:
                x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                if len(x) == 0:
                    return
                self.chunks += 1
                now = time.time()
                if now - self.last_dbg >= 30.0:
                    print(f"📊 {self.chunks} 块 | 状态={['IDLE','LISTEN','THINK','SPEAK'][self.state]} | 录制中={self.recording}", flush=True)
                    self.last_dbg = now
                if self.state not in (IDLE, LISTENING):
                    self.recording = False
                    self.buf = []
                    self._barge_watch(x, now)
                    return
                # LISTENING 超时（等指令超过 12 秒）→ 回 IDLE 并熄灯
                if (self.state == LISTENING and not self.recording
                        and now - getattr(self, "listen_start", now)
                        > getattr(self, "listen_window", 12.0)):
                    print("⏰ 聆听超时，退出", flush=True)
                    self.state = IDLE
                    self.led_mode = "off"
                    self._schedule(self._led_clear_all())
                    self._schedule(self._restore_native())
                    return
                rms = float(np.sqrt(np.mean(x ** 2)))
                self.last_rms = rms
                if self.silero is not None:
                    p_vad = self.silero.prob(x)
                    voiced = rms > 0.0004 and p_vad > 0.40
                else:
                    voiced = rms > 0.0008
                if not self.recording:
                    if voiced:
                        self.recording = True
                        self.buf = [x]
                        self.buf_start = self.last_voice = now
                    return
                # 录制中
                self.buf.append(x)
                if voiced:
                    self.last_voice = now
                dur = now - self.buf_start
                silence = now - self.last_voice
                if (silence > SILENCE_SEC and dur > MIN_UTT_SEC) or dur > MAX_UTT_SEC:
                    audio = np.concatenate(self.buf)
                    self.recording = False
                    self.buf = []
                    if dur >= MIN_UTT_SEC:
                        threading.Thread(
                            target=self._process_utterance,
                            args=(audio, self.state), daemon=True
                        ).start()
            except Exception:
                traceback.print_exc()

    def _process_utterance(self, audio: np.ndarray, state: int):
        try:
            peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
            norm = peak_norm(audio)
            dur = len(audio) / SR
            # 回声抑制：语句起点落在抑制窗口内（原生小爱残余/我们自己应答的回声）直接丢弃
            if getattr(self, "echo_guard_until", 0) and (time.time() - dur) < self.echo_guard_until:
                print(f"🔇 回声抑制: 丢弃{dur:.1f}s音频", flush=True)
                return
            print(f"📈 句长{dur:.1f}s 峰值={peak:.4f} 状态={['IDLE','LISTEN','THINK','SPEAK'][state]}", flush=True)
            if state == IDLE:
                if dur < 0.7:
                    return
                # 音乐歌词兜底：长音频（>8s）几乎不可能是指令，直接跳过省 CPU
                if dur > 8.0:
                    print(f"🎵 长音频跳过({dur:.1f}s，疑似音乐)", flush=True)
                    return
                if self.music_playing and dur > 3.0:
                    print(f"🎵 放歌中跳过歌词({dur:.1f}s)", flush=True)
                    return
                hit = kws_detect(norm)
                print(f"🔍 整句KWS({dur:.1f}s): {hit!r}", flush=True)
                if hit:
                    print(f"🎯 KWS唤醒命中: {hit}", flush=True)
                    self._schedule(self._wake(""))
                    return
                # KWS 未命中则走 ASR 文本匹配
                text = asr_transcribe(norm)
                print(f"🗣️ 整句ASR: {text!r}", flush=True)
                if is_wake_text(text):
                    print(f"🎯 ASR唤醒命中: {text!r}", flush=True)
                    self._schedule(self._wake(""))
                else:
                    path = f"/tmp/vwsamples/idle_{int(time.time())}.wav"
                    os.makedirs("/tmp/vwsamples", exist_ok=True)
                    pcm = (np.clip(norm, -1, 1) * 32767).astype(np.int16)
                    with wave.open(path, "wb") as f:
                        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SR)
                        f.writeframes(pcm.tobytes())
            else:  # LISTENING
                if dur < 0.5:
                    return
                text = asr_transcribe(norm)
                print(f"🎤 整句ASR({dur:.1f}s): {text!r}", flush=True)
                if text:
                    self.state = THINKING
                    self._schedule(self._execute(text))
                else:
                    print("⚠️ 未识别到内容，继续等待", flush=True)
        except Exception:
            traceback.print_exc()

    # ---------------- 原生 ASR 第二通道 ----------------
    def on_native_asr(self, text: str):
        if self.state != IDLE or not text:
            return
        low = text.lower()
        pos = -1
        for pat in WAKE_PATTERNS:
            i = low.find(pat)
            if i >= 0:
                pos = max(pos, i + len(pat))
        if pos < 0:
            return
        cmd = re.sub(r"^[\s，,。.!！？?、]+", "", text[pos:]).strip()
        print(f"🎯 原生ASR唤醒 | 文本={text!r} | 命令={cmd!r}", flush=True)
        self._schedule(self._wake(cmd))

    # ---------------- 状态机 ----------------
    def _schedule(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _arm_listening(self):
        self.recording = False
        self.buf = []
        self.listen_start = time.time()
        self.listen_window = 12.0
        self.led_mode = "listen"   # 聆听中：灯环跟随你的音量
        self.state = LISTENING

    async def _wake(self, command: str):
        if self.state != IDLE:
            return
        # 1) 掐断期间的残余回声（短窗口）
        self.echo_guard_until = time.time() + 2.0
        # 2) kill 原生语音服务 = 真正的秒断（原生 TTS 挂在这个进程里），
        #    并等服务重启回来，之后才能安全播放我们自己的 TTS
        await self._abort_native()
        self.led_mode = "breath"
        if command:
            self.state = THINKING
            await self._execute(command)
        else:
            await self._tts_quick("Hermes在")
            # 3) 只护我们自己应答的尾音（1.2s），不影响紧跟着的追问
            self.echo_guard_until = time.time() + 1.2
            self._arm_listening()

    def _barge_watch(self, x: np.ndarray, now: float):
        """播放中监测打断：自适应回声阈值，超阈持续 0.35s 判定为人声打断"""
        if not self.playing:
            return
        dt = len(x) / SR
        rms = float(np.sqrt(np.mean(x ** 2)))
        self.bar_echo = 0.85 * self.bar_echo + 0.15 * rms
        if now - self.play_start < 1.2:
            return  # 起播瞬态不判定
        if self.bar_echo < 0.004:
            # TTS 停顿窗口：音箱没出声，silero 判定可信——
            # 人声（高分）触发，撞击/噪音（低分）过滤
            p = self.silero.prob(x) if self.silero else 1.0
            hit = rms > 0.018 and p > 0.5
        else:
            # TTS 正在念：silero 无法区分回声和人声（都是人声），靠自适应音量跳变
            hit = rms > max(0.028, self.bar_echo * 2.5)
        if hit:
            self.bar_hits += dt
        else:
            self.bar_hits = 0.0
        if self.bar_hits >= 0.35:
            self.bar_hits = 0.0
            print("✋ 检测到打断", flush=True)
            self._schedule(self._barge_in())

    async def _restore_native(self):
        """对话结束回 IDLE 后重启 pns 音频前端：唤醒流程 kill mico_aivs_lab 后，
        mipns-xiaomi 会卡在对话模式不再检测「小爱同学」唤醒，重启 pns 即恢复"""
        try:
            await open_xiaoai_server.run_shell(
                "/etc/init.d/pns restart >/dev/null 2>&1; "
                "sleep 2; /etc/init.d/mico_aivs_lab start >/dev/null 2>&1", 20000
            )
        except Exception:
            pass

    async def _barge_in(self):
        """掐掉播放：SPEAKING→转入聆听收后半句；THINKING→只掐进度播报"""
        self.playback_killed = True
        try:
            await open_xiaoai_server.run_shell(
                "killall -9 miplayer 2>/dev/null; mphelper pause 2>/dev/null", 3000
            )
        except Exception:
            pass
        if self.state != SPEAKING:
            return
        print("🛑 播放被切断，转入聆听", flush=True)
        await self._led_clear_all()
        self.recording = False
        self.buf = []
        self.listen_start = time.time()
        self.listen_window = 8.0
        self.echo_guard_until = time.time() + 1.5
        self.led_mode = "listen"
        self.state = LISTENING

    async def _abort_native(self):
        """kill -9 原生语音服务（立刻静音），停播+取消唤醒态，等服务重启回来"""
        try:
            await open_xiaoai_server.run_shell(
                "killall -9 mico_aivs_lab >/dev/null 2>&1; "
                "ubus call mediaplayer player_play_operation '{\"action\":\"stop\"}' >/dev/null 2>&1; "
                "ubus call pnshelper event_notify '{\"src\":3,\"event\":7}' >/dev/null 2>&1; "
                "ubus call pnshelper event_notify '{\"src\":3,\"event\":8}' >/dev/null 2>&1; "
                "mphelper pause >/dev/null 2>&1; "
                "/etc/init.d/mico_aivs_lab start >/dev/null 2>&1; "
                "i=0; while [ $i -lt 40 ]; do pgrep mico_aivs_lab >/dev/null && break; sleep 0.25; i=$((i+1)); done; "
                "sleep 1.2",
                15000
            )
        except Exception:
            pass

    def _led_set(self, level: int):
        """level 0=熄灭，1-100=亮度"""
        try:
            if level <= 0:
                # led shut 需指定具体 id，否则栈里的动画关不掉
                last = getattr(self, "led_last_l", 0)
                cmd = (f'ubus call led shut \'{{"L":{last}}}\'; '
                       f'ubus call led shut \'{{"L":0}}\'')
            else:
                self.led_last_l = level
                cmd = f'ubus call led show \'{{"L":{level},"pos":0}}\''
            asyncio.run_coroutine_threadsafe(
                open_xiaoai_server.run_shell(cmd, 3000), self.loop
            )
        except Exception:
            pass

    async def _led_clear_all(self):
        """彻底熄灭：读 status 拿到栈里所有 id，逐个 shut"""
        try:
            res = await open_xiaoai_server.run_shell("ubus call led status", 3000)
            ids = set(re.findall(r"(\d+)\(0\)", getattr(res, "stdout", "") or ""))
            if ids:
                cmds = "".join(f"ubus call led shut '{{\"L\":{i}}}'; " for i in ids)
                await open_xiaoai_server.run_shell(cmds + "ubus call led shut '{\"L\":0}'", 5000)
            else:
                await open_xiaoai_server.run_shell("ubus call led shut '{\"L\":0}'", 3000)
        except Exception:
            pass

    def _led_loop(self):
        """灯环动画线程：listen=跟随麦克风音量，breath=呼吸，off=熄灭"""
        last = -1
        phase = 0
        while True:
            try:
                time.sleep(0.18)
                mode = self.led_mode
                if mode == "off":
                    if last != 0:
                        self._led_set(0)
                        last = 0
                    continue
                if mode == "listen":
                    # 音量跟随：麦克风 RMS → 亮度（说话时灯环脉动）
                    lv = int(min(100, max(8, self.last_rms * 9000)))
                else:  # breath
                    phase = (phase + 1) % 10
                    lv = int(25 + 65 * (1 - abs(5 - phase) / 5))
                if abs(lv - last) >= 10:
                    self._led_set(lv)
                    last = lv
            except Exception:
                time.sleep(0.5)

    async def _speak(self, text: str, next_state: int = IDLE):
        self.state = SPEAKING
        # 播放期间放手：原生 TTS 自己驱动"中间扩散脉冲"灯效，我们不要抢
        self.led_mode = "off"
        self.playing = True
        self.play_start = time.time()
        self.bar_echo = 0.0
        self.playback_killed = False
        try:
            safe = (text or "").replace("'", "'\\''")
            await open_xiaoai_server.run_shell(f"/usr/sbin/tts_play.sh '{safe}'", 120 * 1000)
            self.playing = False
            if self.playback_killed:
                print("🔇 回答被用户打断，跳过收尾", flush=True)
                return
            await asyncio.sleep(0.8)
        except Exception:
            traceback.print_exc()
        self.playing = False
        # 回复结束：留 8 秒跟进窗口，用户可直接追问（多轮对话）
        await self._led_clear_all()
        self.recording = False
        self.buf = []
        self.listen_start = time.time()
        self.listen_window = 8.0
        self.led_mode = "listen"
        self.state = LISTENING

    async def _execute(self, text: str):
        self.led_mode = "breath"
        if not text:
            await self._speak("没听清，再说一次")
            return
        # 立即回应一声，消除 Hermes 思考期间的沉默
        try:
            await open_xiaoai_server.run_shell("/usr/sbin/tts_play.sh '好的，我这就处理'", 15000)
        except Exception:
            pass
        reply = await self._run_hermes(text)
        await self._speak(reply)

    async def _run_hermes(self, text: str) -> str:
        print(f"🤖 Hermes 执行: {text!r}", flush=True)
        prompt = (
            f"{text}\n\n"
            "（这是语音对话场景，请用口语直接回答，100字以内，"
            "不要markdown格式、不要列表符号、不要链接）"
        )

        def _api_stream():
            """流式请求：边收边报进度（工具调用时播报，避免长时间无声等待）"""
            import urllib.request
            msgs = list(self.history[-6:]) + [{"role": "user", "content": prompt}]
            body = json.dumps({
                "model": "hermes-agent",
                "messages": msgs,
                "model_options": {"reasoning": {"enabled": False}},
                "stream": True,
            }).encode()
            req = urllib.request.Request(
                VOICE_API_URL, data=body,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            content = []
            t_start = time.time()
            last_report = 0.0
            spoken = {}
            ev_name = ""
            hb = {"done": False, "last_say": time.time()}
            def _heartbeat():
                i = 0
                while not hb["done"]:
                    time.sleep(2)
                    if hb["done"]:
                        break
                    if time.time() - hb["last_say"] > 8.0:
                        phrase = HEARTBEAT_PHRASES[i % len(HEARTBEAT_PHRASES)]
                        i += 1
                        hb["last_say"] = time.time()
                        print(f"📢 心跳: {phrase}", flush=True)
                        try:
                            asyncio.run_coroutine_threadsafe(self._tts_quick(phrase), self.loop)
                        except Exception:
                            pass
            threading.Thread(target=_heartbeat, daemon=True).start()
            try:
              with urllib.request.urlopen(req, timeout=180) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        ev_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    cur_event = ev_name
                    ev_name = ""   # event 只对下一条 data 有效
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        d = json.loads(data)
                    except Exception:
                        continue
                    if cur_event == "hermes.tool.progress":
                        tool = str(d.get("tool") or "").lower()
                        label = str(d.get("label") or "").strip()
                        verb = TOOL_VERB.get(tool, "正在处理")
                        # 命令/代码类 label 不适合朗读：含空格、符号、纯 ASCII 的一律用泛称
                        speakable = (
                            label
                            and len(label) <= 10
                            and not any(c in label for c in " /\\|$`'\"()<>;&")
                            and not label.isascii()
                            and not label.startswith("call_")
                        )
                        phrase = f"{verb}{label}" if speakable else verb
                        now = time.time()
                        same_recent = (phrase in spoken and now - spoken[phrase] < 30.0)
                        if (not same_recent and now - last_report > 5.0
                                and now - t_start > 1.5 and len(spoken) < 6):
                            spoken[phrase] = now
                            last_report = now
                            hb["last_say"] = now
                            print(f"📢 进度: {phrase}", flush=True)
                            asyncio.run_coroutine_threadsafe(self._tts_quick(phrase), self.loop)
                        continue
                    # 正文增量
                    try:
                        delta = d["choices"][0].get("delta", {}).get("content")
                    except Exception:
                        delta = None
                    if delta:
                        content.append(delta)
            finally:
                hb["done"] = True
            final = "".join(content)
            if final.strip():
                self.history.append({"role": "user", "content": prompt})
                self.history.append({"role": "assistant", "content": final[:800]})
                self.history = self.history[-8:]
            return final

        def _run():
            # 优先走常驻 API Server（快 2-3 倍），失败回退 hermes -z
            try:
                out = (_api_stream() or "").strip()
                if out:
                    out = self._clean_for_tts(out)
                    print(f"💬 [API {len(out)}字] {out[:60]}...", flush=True)
                    return out
            except Exception as e:
                print(f"⚠️ API 失败({e})，回退 hermes -z", flush=True)
            try:
                r = subprocess.run(
                    ["hermes", "-z", prompt],
                    capture_output=True, text=True, timeout=180,
                    cwd=os.path.expanduser("~"),
                    env={**os.environ, "NO_COLOR": "1"},
                )
                out = (r.stdout or "").strip() or "任务完成，但没拿到结果。"
                out = self._clean_for_tts(out)
                print(f"💬 [CLI {len(out)}字] {out[:60]}...", flush=True)
                return out
            except subprocess.TimeoutExpired:
                return "任务超时了。"
            except Exception as e:
                return f"执行出错：{e}"

        return await asyncio.get_event_loop().run_in_executor(None, _run)

    async def _tts_quick(self, text: str, retries: int = 1):
        """短句快速播报；失败自动重试（TTS 服务刚重启时可能未就绪）"""
        safe = (text or "").replace("'", "'\\''")
        for attempt in range(retries + 1):
            try:
                self.playing = True
                self.play_start = time.time()
                self.bar_echo = 0.0
                self.playback_killed = False
                res = await open_xiaoai_server.run_shell(
                    f"/usr/sbin/tts_play.sh '{safe}'", 20000
                )
                if self.playback_killed:
                    print("🔇 TTS 被打断", flush=True)
                    return
                out = (getattr(res, "stdout", "") or "").strip()
                if "exit code: 0" in out or out == "":
                    print(f"🔊 TTS[{attempt}]: {text[:20]}", flush=True)
                    self.playing = False
                    return
                print(f"⚠️ TTS[{attempt}] 异常: {out[-160:]}", flush=True)
            except Exception as e:
                print(f"⚠️ TTS[{attempt}] 失败: {e}", flush=True)
            await asyncio.sleep(1.0)


    @staticmethod
    def _clean_for_tts(s: str) -> str:
        """清理 markdown，限制播报长度"""
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"[#*`>_~\-]{1,}", " ", s)
        s = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", s)
        s = re.sub(r"https?://\S+", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > 220:
            cut = s[:220]
            for sep in ("。", "！", "？", "；"):
                i = cut.rfind(sep)
                if i > 100:
                    cut = cut[:i + 1]
                    break
            s = cut
        return s


bridge = Bridge()


def on_event(event: str):
    try:
        ev = json.loads(event)
        newline = ev.get("data", {}).get("NewLine")
        if not newline:
            return
        if ev.get("event") == "playing":
            st = str(ev.get("data", "")).strip().lower()
            if "playing" in st or "play" == st:
                bridge.music_playing = True
            elif st in ("idle", "pause", "paused", "stop", "stopped"):
                bridge.music_playing = False
            print(f"🎵 播放状态: {st} (music_playing={bridge.music_playing})", flush=True)
            return
        line = json.loads(newline)
        header = line.get("header", {})
        if header.get("namespace") == "SpeechRecognizer" and header.get("name") == "RecognizeResult":
            payload = line.get("payload", {})
            if not payload.get("is_final"):
                return
            results = payload.get("results") or [{}]
            text = results[0].get("text") or results[0].get("origin_text") or ""
            if text:
                print(f"🎧 原生ASR(final): {text!r}", flush=True)
                bridge.on_native_asr(text)
    except Exception:
        pass


async def main():
    bridge.loop = asyncio.get_event_loop()
    threading.Thread(target=bridge._led_loop, daemon=True).start()
    open_xiaoai_server.register_fn("on_input_data", bridge.on_input)
    open_xiaoai_server.register_fn("on_event", on_event)
    print("🔌 监听 ws://0.0.0.0:4399 ...", flush=True)
    await open_xiaoai_server.start_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 退出", flush=True)

# 架构与踩坑精华

状态机：`IDLE → (唤醒) → LISTENING → (指令) → THINKING → (回复) → SPEAKING → LISTENING(8s跟进窗) → IDLE`

## 音频链路

- 音箱 client 经 WebSocket 推 16kHz PCM + 原生小爱 ASR 事件（on_event）到服务端
- **音箱 mic 电平极低**（峰值 RMS 0.004-0.008 ≈ -42dB），KWS/ASR 对绝对电平敏感——必须**整句捕获后峰值归一到 0.7** 再检测，流式逐块喂必失灵（实测多种 AGC 方案全败）
- 门控：RMS 预筛 + silero VAD 神经确认（模型在 xiaozhi models.zip 里自带），双重过滤噪声

## 唤醒匹配（对 ASR 转写的大海捞针）

「转Hermes」的真实转写样本：`转 Hermes / 可以 HERMERS / 嘿 COMERCE / 转横麦 / 转和卖 / 配合莫斯 / 想喝没 / joker miss / 转 Home`……
策略三层：

1. 正则枚举实测变体（`homes?` 单复数都要、`joker\s*mi` 这类怪转写）
2. **pypinyin 声母模糊兜底**：ASR 听成任何汉字组合，声母序列形状稳定（zhm/xhm/hms/hrm/rms/kms）——未知新变体自动接住，不用逐个打补丁
3. 误触回归：转账给朋友/转到第二集/我想喝奶茶 等正常语句不得命中（漏触代价 > 误触代价）

## 延迟：为什么不用 hermes -z

子进程冷启动 8-12s（最坏 71s）。用常驻 Hermes API Server（OpenAI 兼容 `/v1/chat/completions`）→ 4-6s。详见仓库根目录的低延迟方案文档思路：

- `stream:true`：`hermes.tool.progress` 事件实时播报工具调用（"正在搜索××"）；静默 >8s 播心跳短语
- `model_options.reasoning.enabled=false` 关闭深度推理
- 多轮：`messages` 带最近 4 轮
- **SSE 解析坑**：`event:` 行只对紧跟的一条 `data:` 有效，处理完必须清空 event 名，否则正文全被吞

## 打断（无 AEC 的"低配"实现）

- AEC 需要参考信号=正在播放的精确 PCM；固件 TTS 的合成音频**拿不到波形**（无法精确对齐），所以做不了真 AEC
- 替代：播放期跟踪回声电平 EMA，`阈值 = max(0.028, 回声×2.5)`，超阈持续 0.35s 判打断 → `killall miplayer` → 转聆听
- TTS 停顿窗口（回声 <0.004）内叠加 silero 判定，过滤关门/拍手类非人声噪音
- 提升空间（实测路线）：固件 TTS 合成会落盘 `/tmp/tts/*.mp3`，读回解码可做参考信号做真 AEC；保护建立需 2-3s 麦克风积累（互相关对齐），之后打断响应 0.3-0.5s

## 与原生小爱共存（双系统）

- 唤醒 Hermes 时必须 kill 对话服务 `mico_aivs_lab`（`mediaplayer stop`/pnshelper 事件掐不住已开始的播报，14s 原生回复照样说完还被本地 ASR 当指令）
- ⚠️ **唤醒检测不住在 mico_aivs_lab**，在 `mipns-xiaomi`（pns 音频前端，366MB）里：kill 后它卡在对话模式，原生唤醒必死。对话结束回 IDLE 时 `/etc/init.d/pns restart` 自动恢复（约 5s）
- 服务重启后原生 TTS 有就绪竞态：等 `pgrep` + 1.2s 缓冲再播，TTS 失败自动重试一次
- 回声抑制窗必须两段且短：掐断前 2.0s + 应答播完后 1.2s。单段长窗会把用户紧跟应答的追问整个吞掉

## 灯效（ledd，UDP 127.0.0.1:7777）

- `ubus call led show {"L":<亮度>,"pos":<位置>}` / `led shut {"L":<值>}`
- L 就是亮度；**关闭必须清空整个灯控栈**（读 `led status` 抓所有 id 逐个 shut + 补 `{"L":0}`，否则残留动画一直跑）
- 播放 TTS 期间不要发灯命令——原生灯效自带"中间向两边扩散"，抢戏会变成亮点左右摇

## 运维

- 音箱断电 → Mac 侧出现幽灵 ESTABLISHED 连接 → 服务端卡死在死 socket（进程活、不处理）。watchdog 用**日志 mtime 冻结 >120s** 检测并强杀重启，比只查进程存活可靠
- 音箱端：init.sh 必须等 `mico_aivs_lab` 就绪再启动 client（抢跑会导致原生 ASR 事件订阅挂掉：音频正常但收不到识别事件）；client 用 run.sh 循环守护（断线 2s 重连）
- 设备是精简 ash：无 nohup/setsid/base64/openssl/GNU timeout；后台化用 `start-stop-daemon -S -b -m -p <pid> -x <脚本>`（路径在 /sbin，`-x` 指脚本本身）；传文件用 `scp -O`（dropbear 无 sftp-server）；busybox timeout 语法是 `timeout -t 8 CMD`

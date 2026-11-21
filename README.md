# Xiaomi Speaker × Hermes 🎙️

把小米智能音箱变成 Hermes Agent 的语音入口：**唤醒 → 对话 → 工具执行 → 进度播报 → 多轮追问**，全程 4-6 秒，且与原生「小爱同学」双系统共存互不干扰。

```
你: 「小爱同学」……「转Hermes」
箱: 「Hermes在」
你: 「这周末天气怎么样」
箱: 「正在搜索……」(工具进度实时播报)
箱: 「周六多云转晴15度，适合带娃出门……」
你: 「那周日呢」(8秒跟进窗内直接追问，无需重新唤醒)
箱: 「周日有雨，建议改室内……」
```

## ⚡ 让你的 Hermes 一键安装

前提：音箱已刷 Open-XiaoAI patch 固件（见[刷机指引](docs/flashing.md)）。然后对你的 Hermes 说：

> 请安装小米音箱语音桥：clone https://github.com/WeilaiSun/xiaomi-speaker-hermes 到本地，阅读 README 后执行 `bash install.sh`。模型走国内镜像失败时用脚本内置的代理回退。装完打印需要我人工补的配置项（API_SERVER_KEY / 音箱 IP）。

install.sh 幂等可重复执行，会自动：装 venv 依赖（阿里云镜像优先）→ 修复 sherpa-onnx dylib → 下载 103MB 模型（带代理回退）→ 写配置 → 启动服务 → 指引音箱端与常驻部署。

## 特性

| 能力 | 说明 |
|---|---|
| 双系统共存 | 「小爱同学」→ 原生小爱（米家生态）；「转Hermes」→ Hermes 全能力，互不干扰 |
| 流式进度播报 | 基于 Hermes API `hermes.tool.progress` 事件，工具调用实时播报（"正在搜索××"），8 秒静默自动心跳 |
| 播放打断 | 说话即停：自适应回声阈值 + TTS 停顿窗口 silero VAD 过滤噪音 |
| 多轮对话 | 回复后 8 秒跟进窗直接追问，服务端携带 4 轮上下文 |
| 唤醒鲁棒 | 原生 ASR 事件 + 本地 ASR 双通道；变体正则 + **拼音声母模糊兜底**（ASR 听成什么都能接住） |
| 灯效反馈 | 聆听→音量脉冲，处理→呼吸，播报→交还原生灯效，结束全栈熄灭 |
| 自愈 | 服务端日志冻结检测（幽灵连接自愈）+ 音箱端 client 自动重连 + 开机自启 |

## 前置条件

1. **硬件**：小米 AI 音箱 Pro（LX06）或 Xiaomi 智能音箱 Pro（OH2P）
2. **刷机**：刷入 Open-XiaoAI patch 固件（解锁 SSH + 本地 client），见 [docs/flashing.md](docs/flashing.md)
3. **Hermes**：Mac/PC 上跑着 Hermes gateway，且已启用 API Server（`.env` 加 `API_SERVER_ENABLED=true`、`API_SERVER_KEY=<密钥>`）
4. **网络**：音箱、服务端同一局域网；Mac 装 `expect`（`brew install expect`，音箱端自动安装用）

## 手动安装（不用一键脚本时）

```bash
git clone https://github.com/WeilaiSun/xiaomi-speaker-hermes.git
cd xiaomi-speaker-hermes
bash install.sh
# 音箱端（先在音箱端执行过固件安装的前提下）
bash scripts/install-to-speaker.sh <音箱IP> <服务端IP>
```

配置项（`~/.hermes/.env`）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `HERMES_API_URL` | Hermes API Server 地址 | `http://127.0.0.1:8642/v1/chat/completions` |
| `HERMES_API_KEY` | API 密钥（不设则读 `API_SERVER_KEY`） | — |
| `HERMES_VOICE_MODELS` | 模型目录 | `~/.hermes/services/xiaomi-speaker-hermes/models` |

## 架构

```
「小爱同学」唤醒 ──→ 原生小爱（米家生态，互不干扰）
「转Hermes」唤醒 ──┐
                   ├─ 音箱 client (WS 16kHz PCM + 原生ASR事件)
                   ▼
        Mac: hermes_server.py（本项目核心）
        ├ silero VAD 门控 → 整句捕获 → 峰值归一化
        ├ 唤醒匹配：正则变体 + 拼音声母模糊兜底
        ├ Hermes API Server（常驻，stream + tool.progress）
        ├ 进度播报/心跳 + TTS播放打断 + 多轮跟进窗
        └ LED 灯效状态机
                   │
                   ▼
        音箱原生 TTS (tts_play.sh) + 灯环 (ledd)
```

唤醒后服务端会 kill 掉原生对话服务（`mico_aivs_lab`，掐断小爱抢话）；对话结束回 IDLE 时自动重启 pns 音频前端恢复原生唤醒——双系统每轮自动复活，无需人工干预。

## 已知限制

- 仅支持 macOS 服务端（Intel/Apple Silicon 均实测）；Windows 需小改（路径/watchdog 的 stat 语法）
- 打断为"低配"实现：换气空档精准、念述中靠音量跳变；完美打断需 AEC（参考 `docs/architecture.md` 的实测路线）
- 刷机仅支持 LX06/OH2P 两款 Pro；固件版本 ≥1.62.2 需用 fork 的 client-patch 自制
- 播放音乐时歌词可能被捕获（有时长/状态过滤，仍偶发）——Music 场景建议走原生小爱

## 致谢

- [open-xiaoai](https://github.com/idootop/open-xiaoai)（@coderzc 维护）—— 设备端 client 与 WebSocket 协议
- [stevenjoezhang/xiaoai-agent](https://github.com/stevenjoezhang/xiaoai-agent) —— 新固件(1.62.2) client-patch 打包
- [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) —— KWS/ASR/VAD 运行时
- [silero](https://github.com/snakers4/silero-vad) —— 神经网络 VAD
- [rui](https://github.com/yes999zc/rui) —— 全自建语音栈路线的对照参考（中文 KWS/AEC 实测数据来源）

## License

MIT —— 自由使用，但刷机有变砖风险，请先读 [docs/flashing.md](docs/flashing.md) 的安全垫说明。

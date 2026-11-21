#!/bin/bash
# 服务端一键安装（macOS）
# 用法：
#   bash install.sh                          # 基础安装（依赖+模型+配置+启动说明）
#   SPEAKER_IP=192.168.1.50 SERVER_IP=192.168.1.10 bash install.sh  # 同时配置音箱端
#   WITH_LAUNCHD=1 bash install.sh           # 安装 launchd 常驻（需在独立终端 bootstrap）
set -e
DIR="$HOME/.hermes/services/xiaomi-speaker-hermes"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_URL="https://github.com/idootop/open-xiaoai/releases/download/vad-kws-models/models.zip"
MODELS_URL_MIRROR="https://gh-proxy.com/${MODELS_URL}"

echo "==> 1/6 目录与依赖检查"
mkdir -p "$DIR"
cp -R "$REPO_DIR/server" "$REPO_DIR/requirements.txt" "$REPO_DIR/scripts" "$DIR/" 2>/dev/null || true
command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
command -v expect >/dev/null || echo "提示: 未装 expect（音箱端自动安装需要，brew install expect）"

echo "==> 2/6 Python 虚拟环境"
if [ ! -x "$DIR/venv/bin/python" ]; then
    python3 -m venv "$DIR/venv"
fi
PIP="$DIR/venv/bin/pip"
"$DIR/venv/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
"$PIP" install -q --upgrade pip
# 国内优先阿里云镜像，失败回退官方源
"$PIP" install -q -r "$REPO_DIR/requirements.txt" -i https://mirrors.aliyun.com/pypi/simple/ \
  || "$PIP" install -q -r "$REPO_DIR/requirements.txt"

echo "==> 3/6 sherpa-onnx dylib 修复（Intel Mac 必需，Apple Silicon 跳过亦可）"
SP=$("$DIR/venv/bin/python" -c "import sherpa_onnx, os; print(os.path.dirname(sherpa_onnx.__file__))")
ORT_LIB="$DIR/venv/lib/python3.*/site-packages/onnxruntime/capi/libonnxruntime*.dylib"
for f in $ORT_LIB; do [ -e "$f" ] && ln -sf "$f" "$SP/lib/" 2>/dev/null; done

echo "==> 4/6 唤醒/ASR 模型下载（约 103MB）"
if [ ! -f "$DIR/models/encoder.onnx" ]; then
    mkdir -p "$DIR/models" /tmp/kwsdl && cd /tmp/kwsdl
    (curl -sL -o models.zip "$MODELS_URL" || curl -sL -o models.zip "$MODELS_URL_MIRROR") \
        || { echo "模型下载失败，请手动从 $MODELS_URL 下载后解压到 $DIR/models/"; exit 1; }
    unzip -qo models.zip
    # zip 内有嵌套 models/ 目录，统一上移
    find . -name "encoder.onnx" -o -name "tokens.txt" -o -name "joiner.onnx" -o -name "decoder.onnx" \
        -o -name "bpe.model" -o -name "silero_vad.onnx" -o -name "keywords.txt" 2>/dev/null \
      | while read -r f; do cp "$f" "$DIR/models/"; done
    cd - >/dev/null
else
    echo "    模型已存在，跳过"
fi

echo "==> 5/6 配置"
ENVF="$HOME/.hermes/.env"
touch "$ENVF"
if ! grep -q "^API_SERVER_KEY=" "$ENVF"; then
    echo "    ⚠️ $ENVF 里没有 API_SERVER_KEY（Hermes API Server 的密钥）"
    echo "       请补一行: API_SERVER_KEY=<你的gateway密钥>"
    echo "       （若还没启用 API Server：.env 里加 API_SERVER_ENABLED=true，然后 hermes gateway）"
fi
grep -q "^HERMES_API_URL=" "$ENVF" || echo 'HERMES_API_URL=http://127.0.0.1:8642/v1/chat/completions' >> "$ENVF"
echo "    HERMES_API_URL 已写入 $ENVF（跨机访问改成 http://<Tailscale或局域网IP>:8642/v1/chat/completions）"

echo "==> 6/6 启动"
"$DIR/venv/bin/python" "$DIR/server/hermes_server.py" > /tmp/hermes-voice.log 2>&1 &
sleep 3
pgrep -f "server/hermes_server.py" >/dev/null && echo "    ✅ 服务已启动（日志 /tmp/hermes-voice.log）" \
    || { echo "    ❌ 启动失败，看日志尾部:"; tail -5 /tmp/hermes-voice.log; exit 1; }

echo "==> 音箱端"
if [ -n "$SPEAKER_IP" ] && [ -n "$SERVER_IP" ]; then
    bash "$REPO_DIR/scripts/install-to-speaker.sh" "$SPEAKER_IP" "$SERVER_IP"
else
    echo "    前提：音箱已刷 Open-XiaoAI patch 固件（见 docs/flashing.md）"
    echo "    运行: bash scripts/install-to-speaker.sh <音箱IP> <本机IP>"
fi

echo "==> 常驻与守护（可选）"
echo "    launchd 常驻（在独立终端执行）:"
echo "      cp $REPO_DIR/templates/com.hermes.voice-bridge.plist ~/Library/LaunchAgents/"
echo "      launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.hermes.voice-bridge.plist"
echo "    每5分钟守护（冻结自愈）：crontab 加一行"
echo "      */5 * * * * bash $DIR/scripts/watchdog.sh $DIR"
if [ "$WITH_LAUNCHD" = "1" ]; then
    cp "$REPO_DIR/templates/com.hermes.voice-bridge.plist" ~/Library/LaunchAgents/ 2>/dev/null || true
    echo "    plist 已拷到 ~/Library/LaunchAgents/（bootstrap 命令见上，需独立终端手动执行）"
fi
echo "完成 ✅  测试：对音箱说「小爱同学」→「转Hermes」→ 说话"

#!/bin/bash
# 服务端守护：进程不在 → 拉起；日志冻结 >120s（幽灵 TCP 连接致服务僵死）→ 强杀重启
# 用法：bash watchdog.sh [工作目录]   （配 cron 每 5 分钟调用）
APP_DIR=${1:-$HOME/.hermes/services/xiaomi-speaker-hermes}
APP="$APP_DIR/server/hermes_server.py"
PY="$APP_DIR/venv/bin/python"
LOG=/tmp/hermes-voice.log

start_bridge() {
    "$PY" - "$PY" "$APP" "$LOG" <<'PYEOF'
import subprocess, sys, os
py, app, log = sys.argv[1], sys.argv[2], sys.argv[3]
out = open(log, "a")
subprocess.Popen([py, app], cwd=os.path.dirname(app),
                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                 start_new_session=True, close_fds=True)
print("spawned detached")
PYEOF
}

PID=$(pgrep -f "server/hermes_server.py" | head -1)

if [ -z "$PID" ]; then
    echo "restart: not running, starting..."
    start_bridge
    sleep 12
    pgrep -f "server/hermes_server.py" >/dev/null && echo "restarted: pid $(pgrep -f 'server/hermes_server.py' | head -1)" \
        || { echo "error: start failed"; tail -5 "$LOG"; exit 1; }
    exit 0
fi

# 冻结检测：音箱断电产生幽灵 TCP 连接会让服务卡死在死 socket 上
# （进程活着、0% CPU、不收新连接不处理音频），日志新鲜度是唯一可靠信号
NOW=$(date +%s)
MTIME=$(stat -f %m "$LOG" 2>/dev/null || echo "$NOW")
AGE=$(( NOW - MTIME ))
if [ "$AGE" -gt 120 ]; then
    echo "frozen: log idle ${AGE}s (pid $PID) — killing stale server"
    kill -9 "$PID" 2>/dev/null
    sleep 2
    start_bridge
    sleep 12
    pgrep -f "server/hermes_server.py" >/dev/null && echo "recovered" || { echo "error: recovery failed"; exit 1; }
else
    echo "ok: running (pid $PID, log age ${AGE}s)"
fi
exit 0

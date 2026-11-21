#!/bin/sh
# client 自动重连循环：断开后 2 秒重试，服务端重启后几乎瞬间恢复
# 部署：scp -O 到音箱 /data/open-xiaoai/run.sh（由 init.sh 拉起）

while true; do
  /data/open-xiaoai/client ws://<SERVER_IP>:4399
  sleep 2
done

#!/bin/bash
# install-to-speaker.sh <音箱IP> <服务端IP>
# 把 init.sh / run.sh / server.txt 推到音箱并启动（需已刷机且 SSH 可达，密码 open-xiaoai）
set -e
SPEAKER_IP=${1:?用法: install-to-speaker.sh <音箱IP> <服务端IP>}
SERVER_IP=${2:?用法: install-to-speaker.sh <音箱IP> <服务端IP>}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAuthentication=no"
PASS=open-xiaoai

run() { expect -c "
set timeout 60
spawn ssh $SSH_OPTS root@$SPEAKER_IP {$1}
expect {
  \"password:\" { send \"$PASS\r\"; exp_continue }
  eof { }
  timeout { puts TIMEOUT; exit 1 }
}" 2>&1 | grep -vE "Warning: Permanently|ROM Type|_+$|^\s*\|"; }

# run.sh 里的占位符替换为实际服务端 IP
sed "s/<SERVER_IP>/$SERVER_IP/" "$SCRIPT_DIR/device/run.sh" > /tmp/run.sh.installer

echo "==> 推送文件"
expect -c "
set timeout 60
spawn scp -O $SSH_OPTS /tmp/run.sh.installer root@$SPEAKER_IP:/data/open-xiaoai/run.sh
expect {
  \"password:\" { send \"$PASS\r\"; exp_continue }
  eof { }
  timeout { puts TIMEOUT; exit 1 }
}" 2>&1 | tail -1
expect -c "
set timeout 60
spawn scp -O $SSH_OPTS "$SCRIPT_DIR/device/init.sh" root@$SPEAKER_IP:/data/init.sh
expect {
  \"password:\" { send \"$PASS\r\"; exp_continue }
  eof { }
  timeout { puts TIMEOUT; exit 1 }
}" 2>&1 | tail -1

echo "==> 写 server.txt 并启动"
run "echo ws://$SERVER_IP:4399 > /data/open-xiaoai/server.txt; chmod +x /data/open-xiaoai/run.sh /data/init.sh; killall client 2>/dev/null; /data/init.sh; sleep 4; ps | grep open-xiaoai/client | grep -v grep && echo SPEAKER_INSTALL_OK"

echo "完成。回到服务端确认日志出现 ✅ 已连接"

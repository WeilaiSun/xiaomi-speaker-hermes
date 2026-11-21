#!/bin/sh
# 音箱开机自启：等原生语音服务就绪后再启动 client
# 原理：client 若抢在原生语音服务(mico_aivs_lab)就绪前启动，原生 ASR 事件订阅会挂掉，
#      症状 = 音频推流正常但收不到「小爱同学，转Hermes」的识别文本。
# 部署：scp -O 到音箱 /data/init.sh（patch 固件开机自动执行）

i=0
while [ $i -lt 90 ]; do
  ps | grep -v grep | grep -q mico_aivs_lab && break
  sleep 1
  i=$((i+1))
done
sleep 5
/sbin/start-stop-daemon -S -b -m -p /tmp/oxc.pid -x /data/open-xiaoai/run.sh

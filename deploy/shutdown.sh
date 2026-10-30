#!/bin/bash
# Richcomm Power Manager -> PVE 优雅关机（先停容器/虚拟机，再关主机）
exec >>/var/log/ups-manager-shutdown.log 2>&1
echo "==== $(date -Is) shutdown.sh 被调用: $* ===="

# 通知 NUT 桥接进程：本机即将关机，让群晖等 NUT 客户端收到 FSD 后一起关机
mkdir -p /run/ups-bridge 2>/dev/null
touch /run/ups-bridge/fsd 2>/dev/null
for i in 1 2 3 4 5; do
  grep -q "FSD" /run/ups-bridge/ups.dev 2>/dev/null && break
  sleep 1
done
# 给群晖等 NUT 客户端留出轮询并开始关机的时间
sleep 10

for id in $(pct list 2>/dev/null | awk 'NR>1 && $2=="running"{print $1}'); do
  echo "$(date -Is) pct shutdown $id"
  pct shutdown "$id" --timeout 120 || pct stop "$id"
done

for id in $(qm list 2>/dev/null | awk 'NR>1 && $3=="running"{print $1}'); do
  echo "$(date -Is) qm shutdown $id"
  qm shutdown "$id" --timeout 180 || qm stop "$id"
done

for i in $(seq 1 96); do
  n=$(qm list 2>/dev/null | awk 'NR>1 && $3=="running"' | wc -l)
  m=$(pct list 2>/dev/null | awk 'NR>1 && $2=="running"' | wc -l)
  [ $((n+m)) -eq 0 ] && break
  sleep 5
done

sync
echo "$(date -Is) 虚拟机已停止，执行 poweroff"
systemctl poweroff

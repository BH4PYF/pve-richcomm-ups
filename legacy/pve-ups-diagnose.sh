#!/bin/bash
#
# PVE / Debian 上 Richcomm Power Manager 读不到 UPS 的只读诊断脚本
# 不会修改任何配置。用法：
#   sudo bash pve-ups-diagnose.sh 2>&1 | tee /tmp/ups-diag.txt
#
set +e

line() { printf '\n===== %s =====\n' "$*"; }

line "OS / PVE"
cat /etc/os-release 2>/dev/null | head -4
command -v pveversion >/dev/null && pveversion -v 2>/dev/null | head -6
uname -a
echo "arch=$(uname -m)"

line "service"
systemctl is-enabled ups-manager.service 2>&1
systemctl is-active ups-manager.service 2>&1
systemctl status ups-manager.service --no-pager -l 2>&1 | head -25
journalctl -u ups-manager.service -n 40 --no-pager 2>&1

line "process / fds"
P=$(systemctl show -p MainPID --value ups-manager.service 2>/dev/null)
echo "MainPID=${P:-none}"
if [ -n "${P:-}" ] && [ "$P" != "0" ]; then
    ls -l /proc/"$P"/fd 2>/dev/null
    echo "wchan=$(cat /proc/"$P"/wchan 2>/dev/null)"
fi
ps -eo pid,ppid,lstart,cmd 2>/dev/null | grep -E '[u]ps_(monitor|status|config)'
ls -ld /etc/ups_manager 2>/dev/null
systemctl list-units --all 2>/dev/null | grep -i ups

line "device nodes"
ls -l /dev/ttyS* /dev/ttyUSB* /dev/ttyACM* /dev/usb/hiddev* /dev/hidraw* 2>/dev/null
ls -l /dev/serial/by-id/ 2>/dev/null

line "serial ports"
cat /proc/tty/driver/serial 2>/dev/null
command -v setserial >/dev/null && setserial -g /dev/ttyS* 2>/dev/null

line "usb"
command -v lsusb >/dev/null && lsusb
command -v udevadm >/dev/null && for d in /dev/ttyUSB0 /dev/ttyACM0; do
    [ -e "$d" ] && { echo "-- $d"; udevadm info -q property -n "$d" 2>/dev/null | grep -E 'ID_VENDOR|ID_MODEL|ID_SERIAL|DEVPATH'; }
done

line "dmesg"
dmesg 2>/dev/null | grep -Ei 'ttyS|ttyUSB|ttyACM|serial|ch341|ftdi|pl2303|cp210|hiddev|usbhid' | tail -40

line "config"
CONF=/opt/ups-manager/TimeCfg.conf
[ -f "$CONF" ] || CONF=/etc/ups_manager/TimeCfg.conf
echo "config=$CONF"
if command -v xxd >/dev/null; then
    xxd "$CONF" 2>/dev/null
else
    od -Ax -tx1z "$CONF" 2>/dev/null
fi

line "i386 runtime"
dpkg --print-foreign-architectures 2>/dev/null
dpkg -l libc6:i386 2>/dev/null | tail -1
command -v file >/dev/null && file /opt/ups-manager/ups_monitor /etc/ups_manager/ups_monitor 2>/dev/null
ldd /opt/ups-manager/ups_monitor 2>/dev/null | head -6

line "recent serial I/O (strace, 2s)"
if command -v strace >/dev/null && [ -n "${P:-}" ] && [ "$P" != "0" ]; then
    timeout 2 strace -f -p "$P" -e trace=openat,read,write,ioctl -s 80 2>&1 | head -60
else
    echo "strace not installed or service not running"
fi

line "NUT"
if command -v upsc >/dev/null; then
    timeout 3 upsc -l 2>&1
    for u in $(timeout 3 upsc -l 2>/dev/null); do
        timeout 3 upsc "$u" 2>&1 | head -20
    done
else
    echo "nut not installed"
fi

line "PVE guests"
command -v qm >/dev/null && qm list 2>/dev/null | head -10
command -v pct >/dev/null && pct list 2>/dev/null | head -10

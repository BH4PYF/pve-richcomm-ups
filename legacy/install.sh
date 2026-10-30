#!/bin/bash
#
# Richcomm Power Manager (upsmanager 1.1, 2005) —— Ubuntu 24.04 适配安装脚本
#
# 背景：
#   原始 Linux 包是 2003 年工具链编译的 32 位 i386 ELF 程序，只依赖
#   libc.so.6 / libpthread.so.0。Ubuntu 24.04 是 x86_64 系统，默认不带
#   32 位运行库，因此需要启用 i386 架构并安装 libc6:i386。
#   原安装脚本面向 Red Hat 9（/etc/rc.d/rc.local、/etc/sysconfig），
#   本脚本改用 systemd 管理。
#
# 用法：
#   sudo ./install.sh
#   可选环境变量：APP_DIR=/opt/ups-manager（安装目录）
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ups-manager}"
SERVICE_NAME="ups-manager.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_DIR="$SCRIPT_DIR/payload"

log()  { printf '\033[1;32m[i]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo $0"
[ -d "$PAYLOAD_DIR" ] || die "缺少 payload 目录：$PAYLOAD_DIR"
command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl；本脚本要求 systemd 环境。"

ARCH="$(uname -m)"
[ "$ARCH" = "x86_64" ] || die "当前架构为 $ARCH。本包是 32 位 x86 程序，仅适配 x86_64 Ubuntu；ARM 需 qemu/box64，未包含在本脚本中。"

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    log "检测到系统：${PRETTY_NAME:-unknown}"
    case "${VERSION_ID:-}" in
        24.04) ;;
        *) warn "本脚本按 Ubuntu 24.04 验证；当前版本为 ${VERSION_ID:-未知}，如遇问题请先检查 32 位运行库。" ;;
    esac
fi

# 旧版（Red Hat 风格）安装残留会影响串口和共享内存，先检查
if [ -d /etc/ups_manager ]; then
    warn "检测到旧版目录 /etc/ups_manager。若其中 ups_monitor 正在运行，请先停止："
    warn "  sudo pkill -f /etc/ups_manager/ups_monitor"
    if pgrep -f "/etc/ups_manager/ups_monitor" >/dev/null 2>&1; then
        die "旧版监控进程仍在运行，请先停止后再安装。"
    fi
fi

# 1. 32 位运行库
if ! dpkg --print-foreign-architectures | grep -qx i386; then
    log "启用 i386 架构 ..."
    dpkg --add-architecture i386
fi
log "安装 32 位 glibc（libc6:i386）..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y libc6:i386

# 2. 停止已有服务
if systemctl list-unit-files "$SERVICE_NAME" >/dev/null 2>&1; then
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
fi

# 3. 安装文件
log "安装到 $APP_DIR ..."
install -d -m 0755 "$APP_DIR"

if [ -f "$APP_DIR/TimeCfg.conf" ]; then
    backup="$APP_DIR/TimeCfg.conf.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$APP_DIR/TimeCfg.conf" "$backup"
    log "已备份原配置：$backup（保留现有配置）"
else
    install -m 0644 "$PAYLOAD_DIR/TimeCfg.conf" "$APP_DIR/TimeCfg.conf"
    log "已写入默认配置（AC 掉电 30s、低电量 30s、通讯 COM1）"
fi

install -m 0755 "$PAYLOAD_DIR/ups_monitor" "$APP_DIR/ups_monitor"
install -m 0755 "$PAYLOAD_DIR/ups_status"  "$APP_DIR/ups_status"
install -m 0755 "$PAYLOAD_DIR/ups_config"  "$APP_DIR/ups_config"
install -m 0755 "$PAYLOAD_DIR/shutdown.sh" "$APP_DIR/shutdown.sh"
install -m 0755 "$PAYLOAD_DIR/SendMSG.sh"  "$APP_DIR/SendMSG.sh"
install -m 0644 "$PAYLOAD_DIR/pm.conf"     "$APP_DIR/pm.conf"
rm -f "$APP_DIR/Monitor.pid"

sed "s|^APP_DIR=.*|APP_DIR=\"$APP_DIR\"|" "$SCRIPT_DIR/ups_manager" \
    > /usr/local/sbin/ups_manager
chmod 0755 /usr/local/sbin/ups_manager

# 4. systemd 服务
log "安装 systemd 服务 ..."
sed "s|@APP_DIR@|$APP_DIR|g" "$SCRIPT_DIR/ups-manager.service.in" \
    > "/etc/systemd/system/$SERVICE_NAME"
chmod 0644 "/etc/systemd/system/$SERVICE_NAME"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

# 5. 结果检查
sleep 1
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "服务已启动：$SERVICE_NAME"
else
    warn "服务未处于 active 状态，请查看：journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

if [ ! -e /dev/ttyS0 ]; then
    warn "未发现 /dev/ttyS0。请确认 UPS 串口线已连接；USB 转串口见下方说明。"
fi

cat <<EOF

安装完成。下一步：
  1) 设置串口/延时： sudo ups_manager config   （C 选择 COM1/COM2，S 保存，Q 退出）
  2) 查看实时状态：  sudo ups_manager status   （Ctrl+C 退出）
  3) 查看服务日志：  sudo journalctl -u $SERVICE_NAME -f
  4) 重启服务：      sudo ups_manager restart

说明与注意：
  - 程序固定使用 2400 8N1，发送 Megatec/Q1 指令（Q1、I、F）；
    COM1 对应 /dev/ttyS0，COM2 对应 /dev/ttyS1。
  - 若使用 USB 转串口（例如 /dev/ttyUSB0）且机器没有真实 COM1，可把设备
    绑定到 /dev/ttyS0：sudo systemctl edit $SERVICE_NAME，在 [Service] 中加
      ExecStartPre=/bin/mount --bind /dev/ttyUSB0 /dev/ttyS0
    然后 sudo systemctl restart $SERVICE_NAME。
  - 若 UPS 是 USB HID（透明串口）模式，请在 ups_manager config 中把 M 改为
    非 0，并确认 /dev/usb/hiddev0 存在。
  - 本程序是 2005 年的专有 32 位程序，以 root 运行并可执行 poweroff；
    建议只在受控/可信的服务器上使用。若 UPS 兼容标准 Megatec/Q1，
    优先考虑 Ubuntu 自带的 NUT（nutdrv_qx），更安全、可维护。
EOF

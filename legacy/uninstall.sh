#!/bin/bash
#
# 卸载 Richcomm Power Manager（Ubuntu 24.04 适配版）
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ups-manager}"
SERVICE_NAME="ups-manager.service"

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo $0" >&2; exit 1; }
[ -n "$APP_DIR" ] && [ "$APP_DIR" != "/" ] || { echo "非法 APP_DIR：$APP_DIR" >&2; exit 1; }

read -r -p "将停止服务并删除 $APP_DIR，确认？[y/N] " ans
case "${ans}" in
    y|Y|yes|YES) ;;
    *) echo "已取消。"; exit 0 ;;
esac

systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE_NAME"
systemctl daemon-reload
rm -f /usr/local/sbin/ups_manager
rm -rf "$APP_DIR"

echo "已卸载。"
echo "提示：i386 运行库未自动移除（可能被其他程序使用）。"
echo "      如确认不再需要 32 位支持，先卸载所有 :i386 包，再移除架构，例如："
echo "        dpkg -l | awk '/:i386/{print \$2}'"
echo "        sudo apt purge <上面列出的包>"
echo "        sudo dpkg --remove-architecture i386"
echo "      旧版 /etc/ups_manager（如果存在）未做改动。"

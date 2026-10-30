#!/bin/sh
# Install/refresh the DingTalk UPS monitor on the PVE host.
# Prerequisite: /etc/ups-dingtalk/dingtalk.env has been copied there (600).
set -eu
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -s /etc/ups-dingtalk/dingtalk.env ]; then
    echo "missing /etc/ups-dingtalk/dingtalk.env" >&2
    exit 1
fi
chown root:root /etc/ups-dingtalk/dingtalk.env
chmod 600 /etc/ups-dingtalk/dingtalk.env

install -d -m 700 /var/lib/ups-dingtalk
install -m 755 "$SRC/dingtalk_monitor.py" /usr/local/lib/ups-pve/dingtalk_monitor.py
install -m 644 "$SRC/ups-dingtalk.service" /etc/systemd/system/ups-dingtalk.service

systemctl daemon-reload
systemctl enable --now ups-dingtalk.service

sleep 2
systemctl --no-pager --full status ups-dingtalk.service | sed -n '1,30p' || true

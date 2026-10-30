#!/bin/sh
# Install the Richcomm UPS -> NUT (dummy-ups) bridge on the PVE host.
set -eu
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install -d -m 755 /usr/local/lib/ups-pve /run/ups-bridge
install -d -m 700 /var/lib/ups-pve

install -m 640 -g nut "$SRC/ups.conf"        /etc/nut/ups.conf
install -m 640 -g nut "$SRC/upsd.conf"       /etc/nut/upsd.conf
install -m 640 -g nut "$SRC/upsd.users"      /etc/nut/upsd.users
if grep -q 'password = CHANGE_ME' /etc/nut/upsd.users 2>/dev/null; then
    echo "WARNING: /etc/nut/upsd.users still has CHANGE_ME." >&2
    echo "         Set a real password before the NAS connects." >&2
fi
install -m 640 -g nut "$SRC/nut.conf"        /etc/nut/nut.conf

install -m 755 "$SRC/nut_bridge.py"          /usr/local/lib/ups-pve/nut_bridge.py
install -m 644 "$SRC/ups-nut-bridge.service" /etc/systemd/system/ups-nut-bridge.service
install -m 755 "$SRC/runtime_test_logger.py" /usr/local/lib/ups-pve/runtime_test_logger.py
install -m 644 "$SRC/ups-runtime-test.service" /etc/systemd/system/ups-runtime-test.service

# Native usbfs poller: replaces the vendor 32-bit ups_monitor.  It claims
# interface 0 through libusb (QEMU's 125 Hz cadence instead of usbhid's
# 250 Hz), publishes /run/ups-poller/state.json for the bridge, and carries
# the TimeCfg AC-fail -> /opt/ups-manager/shutdown.sh chain itself, so the
# vendor program stays disabled.  /opt/ups-manager must still exist for
# TimeCfg.conf and shutdown.sh.
install -m 755 "$SRC/ups-poller"             /usr/local/sbin/ups-poller
install -m 644 "$SRC/ups-poller.service"     /etc/systemd/system/ups-poller.service
install -m 644 "$SRC/ups-usb-added.service"  /etc/systemd/system/ups-usb-added.service
install -m 644 "$SRC/98-ups-usb-added.rules" /etc/udev/rules.d/98-ups-usb-added.rules
install -m 644 "$SRC/99-ups-nosuspend.rules" /etc/udev/rules.d/99-ups-nosuspend.rules

# Health check / incident capture, and the on-demand self-test helpers
# (T/TL/CT and the timed T10 form; watch + guard cancel a long test).
install -m 755 "$SRC/ups-linkcheck"          /usr/local/sbin/ups-linkcheck
install -m 644 "$SRC/ups-linkcheck.service"  /etc/systemd/system/ups-linkcheck.service
install -m 644 "$SRC/ups-linkcheck.timer"    /etc/systemd/system/ups-linkcheck.timer

# Diagnostics that stay on: USB state vs NIC link correlation, and a usbmon
# trace kept as wire evidence for future link drops.  Both are read-only.
install -m 755 "$SRC/ups-net-watch"          /usr/local/sbin/ups-net-watch
install -m 644 "$SRC/ups-net-watch.service"  /etc/systemd/system/ups-net-watch.service
install -m 755 "$SRC/usbmon-ups.py"          /usr/local/lib/ups-pve/usbmon-ups.py
install -m 644 "$SRC/ups-usbmon.service"     /etc/systemd/system/ups-usbmon.service
install -m 755 "$SRC/ups-selftest.py"        /usr/local/sbin/ups-selftest
install -m 755 "$SRC/ups-selftest-watch.py"  /usr/local/sbin/ups-selftest-watch
install -m 755 "$SRC/ups-selftest-guard.py"  /usr/local/sbin/ups-selftest-guard
install -m 755 "$SRC/ups-autoct"             /usr/local/sbin/ups-autoct

# UPS status row in the PVE node summary; the APT hook re-applies it after
# pve-manager upgrades (which replace index.html.tpl).
install -m 644 "$SRC/pve-ups-status.js"  /usr/local/lib/ups-pve/pve-ups-status.js
install -m 755 "$SRC/patch-pve-gui.sh"   /usr/local/lib/ups-pve/patch-pve-gui.sh
install -m 644 "$SRC/apt-pve-ups-gui.conf" /etc/apt/apt.conf.d/99pve-ups-gui
/usr/local/lib/ups-pve/patch-pve-gui.sh

install -d -m 755 /etc/ups-pve
if [ -f /etc/ups-pve/runtime.conf ]; then
    echo "keeping existing /etc/ups-pve/runtime.conf"
else
    install -m 644 "$SRC/runtime.conf" /etc/ups-pve/runtime.conf
fi

# dummy-ups refuses to start when its definition file is still missing, which
# can happen right after boot while the bridge is producing the first frame.
install -d -m 755 /etc/systemd/system/nut-driver@ups.service.d
cat > /etc/systemd/system/nut-driver@ups.service.d/wait-bridge.conf <<'EOF'
[Unit]
After=ups-nut-bridge.service
Wants=ups-nut-bridge.service

[Service]
ExecStartPre=/bin/sh -c 'for i in $(seq 1 30); do [ -s /run/ups-bridge/ups.dev ] && exit 0; sleep 1; done; exit 0'
EOF

# The NAS is the only remote consumer; upsmon on this host would duplicate
# the vendor program's own shutdown logic, so keep it disabled.
systemctl disable --now nut-monitor.service 2>/dev/null || true

systemctl daemon-reload
# The poller and the vendor daemon both want interface 0; the unit declares
# Conflicts=ups-manager, this just makes the choice explicit for old hosts.
systemctl disable --now ups-manager.service 2>/dev/null || true
systemctl enable ups-poller.service
systemctl enable ups-nut-bridge.service
systemctl restart ups-poller.service
systemctl restart ups-nut-bridge.service
systemctl enable --now ups-linkcheck.timer
systemctl enable --now ups-net-watch.service ups-usbmon.service
udevadm control --reload-rules 2>/dev/null || true

# The runtime test logger is started by hand for a discharge test only.
systemctl disable ups-runtime-test.service 2>/dev/null || true

# Rebuild nut-driver@*.service instances from ups.conf, then start them.
systemctl restart nut-driver-enumerator.service
systemctl enable --now nut-server.service
systemctl restart nut-driver@ups.service

sleep 2
systemctl --no-pager --full status nut-driver@ups.service ups-nut-bridge.service nut-server.service | sed -n '1,80p' || true

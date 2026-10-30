#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One line per minute of UPS link state, for an overnight stability soak.

It never touches the USB device: everything comes from the poller's state
file, sysfs, systemctl and upsc, so it cannot disturb the poller it watches.

/var/log/ups-soak.log gets
    time dev state_age input block load bits gauge nut poller_pid drops
every minute, a LOST / BACK marker when the device leaves or returns, and an
hourly summary line.  'drops' counts presence transitions since this process
started; full evidence on a drop is still captured by ups-linkcheck.

Start it for a soak with `systemctl start ups-soak` (the unit is not enabled),
read the file with `tail -f`, stop it when the soak is over.
"""

import json
import os
import subprocess
import time
from datetime import datetime

STATE = "/run/ups-poller/state.json"
LOG = "/var/log/ups-soak.log"
INTERVAL = 60.0
USB_IDS = "/sys/bus/usb/devices"


def read_state():
    try:
        with open(STATE, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    try:
        state["age"] = round(time.time() - float(state.get("ts") or 0), 1)
    except (TypeError, ValueError):
        state["age"] = None
    return state


def device():
    """(busnum, devnum) of 0925:1234 from sysfs, or (None, None)."""
    try:
        names = os.listdir(USB_IDS)
    except OSError:
        return None, None
    for name in names:
        base = os.path.join(USB_IDS, name)
        try:
            with open(os.path.join(base, "idVendor")) as handle:
                vid = handle.read().strip()
            with open(os.path.join(base, "idProduct")) as handle:
                pid = handle.read().strip()
        except OSError:
            continue
        if vid == "0925" and pid == "1234":
            def number(field):
                try:
                    with open(os.path.join(base, field)) as handle:
                        return int(handle.read().strip())
                except (OSError, ValueError):
                    return None
            return number("busnum"), number("devnum")
    return None, None


def nut_status():
    try:
        proc = subprocess.run(["upsc", "ups"], capture_output=True, text=True,
                              timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    for line in proc.stdout.splitlines():
        if line.startswith("ups.status:"):
            return line.split(":", 1)[1].strip()
    return "?"


def poller_pid():
    try:
        proc = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value",
                               "ups-poller"], capture_output=True, text=True,
                              timeout=10)
        return proc.stdout.strip() or "?"
    except (OSError, subprocess.TimeoutExpired):
        return "?"


def log(line):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write("%s %s\n" % (stamp, line))


def main():
    seen = None
    gone_at = None
    drops = 0
    lines = 0
    while True:
        now = time.time()
        state = read_state()
        bus, dev = device()
        present = dev is not None
        if seen is not None and present != seen:
            if present:
                log("BACK: device %s:%s re-enumerated after %.1f min"
                    % (bus, dev, (now - gone_at) / 60.0))
            else:
                gone_at = now
                drops += 1
                log("LOST: device disappeared from the bus (drop #%d)" % drops)
        seen = present
        fields = [
            "dev=%s:%s" % (bus, dev) if present else "dev=absent",
            "age=%s" % (state["age"] if state else "-"),
            "input=%s" % (state.get("input_voltage") if state else "-"),
            "block=%s" % (state.get("battery_block") if state else "-"),
            "load=%s" % (state.get("load") if state else "-"),
            "bits=%s" % (state.get("bits") if state else "-"),
            "gauge=%s" % (round(state["gauge"], 1)
                          if state and state.get("gauge") is not None else "-"),
            "nut=%s" % nut_status(),
            "poller=%s" % poller_pid(),
            "drops=%d" % drops,
        ]
        log(" ".join(fields))
        lines += 1
        if lines % 60 == 0:
            log("HOUR SUMMARY: %d samples, %d drop(s) since start" % (lines, drops))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

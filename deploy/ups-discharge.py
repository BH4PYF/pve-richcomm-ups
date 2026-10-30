#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""10 s CSV log for a real mains-cut discharge test.

Reads only the poller state file, the bridge's SOC file and `upsc ups`, so it
never touches the USB device.  One row every INTERVAL seconds:

    ts iso input block pack load temp bits gauge soc runtime status age

Start it for the test with
    systemd-run --unit=ups-discharge --collect /usr/local/sbin/ups-discharge
and stop it with `systemctl stop ups-discharge`.  Log: /var/log/ups-discharge.csv
"""

import json
import os
import subprocess
import time
from datetime import datetime

STATE = "/run/ups-poller/state.json"
BATTERY = "/var/lib/ups-pve/battery.json"
LOG = "/var/log/ups-discharge.csv"
INTERVAL = 10.0
HEADER = ("ts,iso,input,block,pack,load,temp,bits,gauge,soc,runtime,status,age\n")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def upsc():
    """(soc, runtime, status) from NUT, best effort."""
    try:
        out = subprocess.run(
            ["upsc", "ups"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "", "", ""
    vals = {}
    for line in out.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            vals[key.strip()] = value.strip()
    return (vals.get("battery.charge", ""), vals.get("battery.runtime", ""),
            vals.get("ups.status", ""))


def fmt(value, spec):
    if value is None or value == "":
        return ""
    try:
        return spec % float(value)
    except (TypeError, ValueError):
        return str(value)


def main():
    new = not os.path.exists(LOG) or os.path.getsize(LOG) == 0
    with open(LOG, "a", encoding="utf-8", buffering=1) as out:
        if new:
            out.write(HEADER)
        while True:
            now = time.time()
            state = read_json(STATE)
            battery = read_json(BATTERY)
            soc, runtime, status = upsc()
            age = None
            try:
                age = now - float(state.get("ts") or 0)
            except (TypeError, ValueError):
                pass
            block = state.get("battery_block")
            pack = None if block is None else block * 2.0
            out.write(",".join([
                "%.3f" % now,
                datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                fmt(state.get("input_voltage"), "%.1f"),
                fmt(block, "%.2f"),
                fmt(pack, "%.2f"),
                fmt(state.get("load"), "%.1f"),
                fmt(state.get("temperature"), "%.1f"),
                "" if state.get("bits") is None else str(state.get("bits")),
                fmt(state.get("gauge"), "%.1f"),
                soc, runtime, status, fmt(age, "%.1f"),
            ]) + "\n")
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

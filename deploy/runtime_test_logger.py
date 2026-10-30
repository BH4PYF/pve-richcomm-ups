#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Log UPS telemetry during a mains-off runtime test.

Every 15 s one CSV row is appended to /var/log/ups-runtime-test.csv so the
discharge curve (voltage / charge / load / estimated runtime) can be
analysed afterwards and turned into RUNTIME_MINUTES in runtime.conf.
"""

import csv
import os
import subprocess
import time
from datetime import datetime

LOG_PATH = "/var/log/ups-runtime-test.csv"
UPSC = "/usr/bin/upsc"
UPS_NAME = "ups"
INTERVAL = 15

FIELDS = [
    "timestamp",
    "status",
    "charge",
    "battery_voltage",
    "load",
    "runtime",
    "input_voltage",
    "temperature",
]


def read_ups():
    result = subprocess.run(
        [UPSC, UPS_NAME],
        capture_output=True,
        text=True,
        timeout=10,
    )
    data = {}
    for line in result.stdout.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            data[key.strip()] = value.strip()
    return data


def main():
    new_file = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
    handle = open(LOG_PATH, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=FIELDS)
    if new_file:
        writer.writeheader()
        handle.flush()

    while True:
        started = time.time()
        row = dict.fromkeys(FIELDS, "")
        row["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            data = read_ups()
            row["status"] = data.get("ups.status", "")
            row["charge"] = data.get("battery.charge", "")
            row["battery_voltage"] = data.get("battery.voltage", "")
            row["load"] = data.get("ups.load", "")
            row["runtime"] = data.get("battery.runtime", "")
            row["input_voltage"] = data.get("input.voltage", "")
            row["temperature"] = data.get("ups.temperature", "")
        except Exception:
            row["status"] = "NOCOMM"
        writer.writerow(row)
        handle.flush()
        time.sleep(max(1.0, INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    main()

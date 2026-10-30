#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record the vendor status *screen* (not just shared memory) on a fixed cycle.

`ups_status` prints the fields the vendor program keeps in shared memory, and
some of them only exist there: the self-test state, the battery/AC/bypass
flags, the vendor charge table and the configured delays.  The 1 s SHM logger
cannot see those, so during a test this recorder takes one screen frame every
CYCLE seconds and writes them next to the raw pack voltage.

One capture costs about 2.5 s (the vendor screen itself), hence the 3 s floor.
Reading the screen never touches the USB device.
"""

import ctypes
import os
import struct
import sys
import time
from datetime import datetime

sys.path.insert(0, "/usr/local/lib/ups-pve")

import nut_bridge  # noqa: E402  (shared helpers: capture/parse/SHM reader)

OUT_PATH = "/var/log/ups-screenlog.csv"
CYCLE = 3.0

HEADER = ",".join(
    [
        "time",
        "epoch",
        "self_test",
        "ac_power",
        "battery_status",
        "boost_buck",
        "charge",
        "load",
        "temp",
        "input_v",
        "output_v",
        "input_freq",
        "block_v",
        "pack_v",
        "warn",
    ]
)


def shm_fields():
    """Per-block voltage and the raw warning word from vendor shared memory."""
    block = ""
    warn = ""
    libc = nut_bridge._libc
    if libc is None:
        return block, warn
    shmid = libc.shmget(nut_bridge.SHM_KEY, 0, 0)
    if shmid < 0:
        return block, warn
    address = libc.shmat(shmid, None, 0)
    if not address or address == ctypes.c_void_p(-1).value:
        return block, warn
    try:
        raw = ctypes.string_at(address, nut_bridge.SHM_SIZE)
    finally:
        libc.shmdt(address)
    block = "%.3f" % struct.unpack_from(
        "<f", raw, nut_bridge.SHM_BATTERY_VOLTAGE_OFFSET
    )[0]
    warn = "0x%04x" % struct.unpack_from("<i", raw, 152)[0]
    return block, warn


def row():
    values = nut_bridge.parse(nut_bridge.capture())
    block, warn = shm_fields()
    pack = ""
    if block:
        pack = "%.2f" % (float(block) * nut_bridge.BATTERY_PACK_MULTIPLIER)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return [
        stamp,
        "%.3f" % time.time(),
        values.get("self_test", ""),
        values.get("ac_power", ""),
        values.get("battery_status", ""),
        values.get("boost_buck", ""),
        values.get("charge", ""),
        values.get("load", ""),
        values.get("temperature", ""),
        values.get("input_voltage", ""),
        values.get("output_voltage", ""),
        values.get("input_frequency", ""),
        block,
        pack,
        warn,
    ]


def main():
    new_file = not os.path.exists(OUT_PATH)
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        if new_file:
            out.write(HEADER + "\n")
            out.flush()
        print("screen logger started: %s every %.0f s" % (OUT_PATH, CYCLE), flush=True)
        while True:
            started = time.time()
            try:
                out.write(",".join(row()) + "\n")
                out.flush()
            except Exception as exc:
                print("sample failed: %s" % exc, flush=True)
            delay = started + CYCLE - time.time()
            if delay > 0:
                time.sleep(delay)


if __name__ == "__main__":
    main()

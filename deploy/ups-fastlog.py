#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Temporary 1 s sampler for the Richcomm UPS shared-memory frame.

The vendor program refreshes its SysV shared memory continuously; reading it
does not touch the USB device.  Every second this logger records the raw
per-block battery voltage, the scaled pack voltage, load, temperature, the
current NUT/web status frame (updated by the bridge every ~12 s) and the full
224-byte shared-memory dump so fields that change during an outage can be
identified afterwards.

Run manually for a test; it stops on Ctrl+C and appends to the CSV.
"""

import ctypes
import json
import re
import struct
import time
from datetime import datetime

SHM_KEY = 0x1B56
SHM_SIZE = 224
SHM_BATTERY_VOLTAGE_OFFSET = 140
SHM_TEMPERATURE_OFFSET = 144
SHM_LOAD_OFFSET = 148
SHM_INPUT_FREQ_OFFSET = 136
PACK_MULTIPLIER = 2.0
RUNTIME_CONF = "/etc/ups-pve/runtime.conf"


def display_offset():
    """VOLTAGE_OFFSET from runtime.conf.

    The logger used to hardcode 0.85 while the bridge had moved to 0.80, so its
    pack_display column sat 0.05 V above every other consumer of the data.
    """
    try:
        with open(RUNTIME_CONF, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("VOLTAGE_OFFSET="):
                    return float(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 0.80


DISPLAY_OFFSET = display_offset()
WEB_STATUS_FILE = "/usr/share/pve-manager/js/ups-status.js"
OUT_PATH = "/var/log/ups-fastlog.csv"
INTERVAL = 1.0

HEADER = (
    "time,epoch,block_v,pack_v,pack_display,load,temp,input_freq,"
    "status,web_ts,charge,runtime,input_v,web_voltage,shm_hex"
)

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
_libc.shmget.restype = ctypes.c_int
_libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
_libc.shmat.restype = ctypes.c_void_p
_libc.shmdt.argtypes = [ctypes.c_void_p]
_libc.shmdt.restype = ctypes.c_int


def read_shm():
    shmid = _libc.shmget(SHM_KEY, 0, 0)
    if shmid < 0:
        return None
    address = _libc.shmat(shmid, None, 0)
    if not address or address == ctypes.c_void_p(-1).value:
        return None
    try:
        return ctypes.string_at(address, SHM_SIZE)
    finally:
        _libc.shmdt(address)


def read_web():
    try:
        with open(WEB_STATUS_FILE, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}
    match = re.search(r"\((\{.*\})\);", text)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except ValueError:
        return {}


def main():
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        out.write(HEADER + "\n")
        out.flush()
        next_tick = time.time()
        while True:
            now = time.time()
            stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            raw = read_shm()
            if raw is None:
                row = [stamp, "%.3f" % now] + [""] * 12 + ["shm-unavailable"]
            else:
                block = struct.unpack_from("<f", raw, SHM_BATTERY_VOLTAGE_OFFSET)[0]
                temp = struct.unpack_from("<f", raw, SHM_TEMPERATURE_OFFSET)[0]
                load = struct.unpack_from("<f", raw, SHM_LOAD_OFFSET)[0]
                freq = struct.unpack_from("<f", raw, SHM_INPUT_FREQ_OFFSET)[0]
                pack = block * PACK_MULTIPLIER
                web = read_web()
                row = [
                    stamp,
                    "%.3f" % now,
                    "%.2f" % block,
                    "%.2f" % pack,
                    "%.2f" % (pack + DISPLAY_OFFSET),
                    "%.1f" % load,
                    "%.1f" % temp,
                    "%.1f" % freq,
                    str(web.get("status", "")),
                    str(web.get("ts", "")),
                    str(web.get("charge", "")),
                    str(web.get("runtime", "")),
                    str(web.get("input_voltage", "")),
                    str(web.get("voltage", "")),
                    raw.hex(),
                ]
            out.write(",".join(row) + "\n")
            out.flush()

            next_tick += INTERVAL
            delay = next_tick - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.time()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Keep a rolling text trace of the UPS USB traffic (bus 1, device 1-14).
#
# This kernel exposes usbmon only through debugfs text streams (no usbmonN
# netdev), so read /sys/kernel/debug/usb/usbmon/1t, keep the lines that belong
# to the UPS's current device address plus bus-level lines (address 0), and
# rotate at 8 MB with four files.
#
# Address field looks like Ii:010:01 (type:devnum:endpoint), devnum in hex.

import os
import re
import sys
import time

SRC = '/sys/kernel/debug/usb/usbmon/1t'
DEVNODE = '/sys/bus/usb/devices/1-14/devnum'
OUT = '/var/log/usbmon-ups.log'
MAX_BYTES = 8 * 1024 * 1024
KEEP = 4
NL = bytes([10])
ADDR = re.compile(rb':([0-9a-fA-F]{3}):[0-9a-fA-F]{2} ')


def devnum():
    try:
        with open(DEVNODE) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def rotate():
    for i in range(KEEP - 1, 0, -1):
        src = '%s.%d' % (OUT, i)
        dst = '%s.%d' % (OUT, i + 1)
        if os.path.exists(src):
            os.replace(src, dst)
    if os.path.exists(OUT):
        os.replace(OUT, OUT + '.1')


def main():
    current = devnum()
    checked = 0.0
    size = os.path.getsize(OUT) if os.path.exists(OUT) else 0
    out = open(OUT, 'ab', buffering=0)
    print('usbmon trace started (dev %s)' % current, flush=True)
    src = open(SRC, 'rb', buffering=0)
    buf = b''
    while True:
        chunk = src.read(8192)
        if not chunk:
            time.sleep(0.2)
            continue
        buf += chunk
        while NL in buf:
            line, buf = buf.split(NL, 1)
            line = line + NL
            if time.time() - checked > 5.0:
                current = devnum()
                checked = time.time()
            match = ADDR.search(line)
            if not match:
                continue
            num = int(match.group(1))  # usbmon prints the device number in decimal
            if current is not None and num not in (0, current):
                continue
            out.write(line)
            size += len(line)
            if size > MAX_BYTES:
                out.close()
                rotate()
                out = open(OUT, 'ab', buffering=0)
                size = 0


if __name__ == '__main__':
    sys.exit(main())

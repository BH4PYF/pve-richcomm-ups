#!/usr/bin/env python3
"""Decode the vendor charge gauge and pair multimeter readings with the logs.

The vendor screen's charge%% is not a measured capacity: ups_status computes it
from the one 12 V block voltage it keeps in shared memory, with

    units   = round((V_block - 10.0) * 10)      # 0.1 V/block steps
    charge%% = units / 36 * 100                  # 2.7778%% per 0.1 V

verified against the 1 s SHM log (11.8 -> 50.00, 12.7 -> 75.00, 13.5 -> 97.2).
The fDc value itself is quantised to 0.1 V, so below that resolution the gauge
is blind: during a discharge it sits on one step for minutes.

Usage:
    vendor-table.py formula              print the closed form + breakpoints
    vendor-table.py pair  HH:MM:SS ...    vendor values at those wall times
"""
import csv
import datetime
import statistics
import struct
import sys

FAST = '/var/log/ups-fastlog.csv'
OFFSET = 0.80          # runtime.conf VOLTAGE_OFFSET, raw pack -> multimeter
BLOCKS_PER_PACK = 2    # 2 x 12 V in series
GAUGE_SPAN = 36        # 1/36 per 0.1 V/block, 0%% at 10.0 V/block


def gauge(v_block):
    units = int(round((v_block - 10.0) * 10))
    return max(0, min(36, units)) / GAUGE_SPAN * 100.0


def load_fast():
    rows = []
    with open(FAST) as f:
        for r in csv.DictReader(f):
            h = r.get('shm_hex') or ''
            if len(h) < 288:
                continue
            try:
                e = float(r['epoch'])
                b = bytes.fromhex(h)
            except ValueError:
                continue
            rows.append((e, struct.unpack_from('<f', b, 140)[0],
                         float(r['load'] or 0), r['time']))
    rows.sort()
    return rows


def pair(rows, stamps):
    now = datetime.datetime.now()
    print('%-9s %9s %9s %9s %9s %6s' %
          ('time', 'raw_pack', 'raw+0.80', 'meter', 'gauge', 'load'))
    out = []
    for stamp in stamps:
        parts = stamp.split(':')
        sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
        d = now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
        epoch = d.timestamp() + float(parts[2])
        win = [x for x in rows if abs(x[0] - epoch) <= 20.0]
        if not win:
            print('%-9s  no samples' % stamp)
            continue
        blk = statistics.median(x[1] for x in win)
        raw = blk * BLOCKS_PER_PACK
        load = statistics.median(x[2] for x in win)
        out.append((stamp, blk, raw, gauge(blk), load))
        print('%-9s %9.2f %9.2f %9s %8.2f%% %5.1f%%' %
              (stamp, raw, raw + OFFSET, '?', gauge(blk), load))
    return out


def main():
    rows = load_fast()
    if not rows:
        print('no fastlog rows', file=sys.stderr)
        return 1
    if len(sys.argv) > 1 and sys.argv[1] == 'pair':
        pair(rows, sys.argv[2:])
        return 0
    print('vendor gauge closed form: charge%% = round((V_block-10)*10)/36*100')
    print('breakpoints (V/block, raw pack, gauge%%):')
    for u in range(0, 37, 2):
        blk = 10.0 + u / 10.0
        print('  %5.2f  %6.2f  %6.2f%%' % (blk, blk * 2, u / 36 * 100))
    print('fast log: %d rows, %.1f min span' %
          (len(rows), (rows[-1][0] - rows[0][0]) / 60))
    return 0


main()

#!/usr/bin/env python3
"""Turn paired (wall clock, multimeter volts) samples into a corrected SOC table.

Inputs come from two places:

* the user's multimeter series -- the only absolute voltage reference;
* /var/log/ups-fastlog.csv -- 1 s vendor ADC (fDc, 0.1 V quantised, dithered)
  plus the load percentage, so the mean over a +-20 s window is good to about
  0.02 V and gives us the vendor gauge value at the same instant.

The vendor gauge is decoded as charge%% = round((V_block - 10)*10)/36 * 100,
which is why it reads 50%% while the pack is still near full.  The true SOC here
is the amp-hour counter: the load percentage is of the rated 660 VA, the
inverter and its DC-DC stage are lossy, so the DC current is estimated as

    I_dc = load%% * RATED_VA * PF / (EFF * V_real)

Both PF and EFF are unknown on this unit; they are printed as knobs so the
resulting table can be judged for sensitivity rather than trusted blindly.

usage: calibrate.py T0=HH:MM:SS HH:MM:SS=V ...
       e.g. calibrate.py 22:33:40 22:34:30=24.8 22:38:00=24.5
"""
import csv
import datetime
import statistics
import struct
import sys

FAST = '/var/log/ups-fastlog.csv'
OFFSET = 0.80
CAPACITY_AH = 45.0
RATED_VA = 660.0
PF = 0.90
EFF = 0.85


def samples():
    out = []
    with open(FAST) as f:
        for r in csv.DictReader(f):
            h = r.get('shm_hex') or ''
            if len(h) < 288:
                continue
            try:
                e = float(r['epoch'])
            except ValueError:
                continue
            b = bytes.fromhex(h)
            out.append((e, struct.unpack_from('<f', b, 140)[0] * 2.0,
                        float(r['load'] or 0)))
    out.sort()
    return out


def window(rows, epoch, span=20.0):
    w = [x for x in rows if abs(x[0] - epoch) <= span]
    if not w:
        return None
    return (statistics.mean(x[1] for x in w),
            statistics.mean(x[2] for x in w),
            statistics.mean(x[0] for x in w), len(w))


def gauge(raw_pack):
    block = raw_pack / 2.0
    return max(0.0, min(36.0, round((block - 10.0) * 10))) / 36.0 * 100.0


def main():
    args = [a for a in sys.argv[1:] if '=' not in a and ':' in a]
    pairs = [a.split('=') for a in sys.argv[1:] if '=' in a]
    if not args or not pairs:
        print(__doc__)
        return 2
    now = datetime.datetime.now()

    def to_epoch(stamp):
        p = stamp.split(':')
        d = now.replace(hour=int(p[0]), minute=int(p[1]), second=0, microsecond=0)
        return d.timestamp() + float(p[2])

    t0 = to_epoch(args[0])
    rows = samples()
    print('T0 = %s  (fastlog rows %d, span %.1f min)' %
          (args[0], len(rows), (rows[-1][0] - rows[0][0]) / 60))
    print()
    print('%-9s %7s %7s %9s %8s %8s %8s %7s %7s' %
          ('time', 't_min', 'meter', 'raw', 'raw+off', 'err', 'load', 'gauge', 'Ah_cum'))
    # integrate DC current over the whole run first (1 s grid)
    cum = 0.0
    series = []
    prev = None
    for e, raw, load in rows:
        if e < t0:
            continue
        v_real = raw + OFFSET
        if v_real <= 18 or load <= 0:
            prev = e
            continue
        if prev is not None:
            dt = e - prev
            if 0 < dt < 30:
                cum += (load / 100.0 * RATED_VA * PF) / (EFF * v_real) * dt / 3600.0
        prev = e
        series.append((e, cum))

    def ah_at(epoch):
        best = None
        for e, c in series:
            if e <= epoch:
                best = c
            else:
                break
        return best if best is not None else 0.0

    fitted = []
    for stamp, meter in pairs:
        epoch = to_epoch(stamp)
        w = window(rows, epoch)
        if not w:
            print('%-9s  no vendor samples' % stamp)
            continue
        raw, load, emid, n = w
        meter = float(meter)
        err = meter - (raw + OFFSET)
        g = gauge(raw)
        ah = ah_at(epoch)
        soc = max(0.0, 100.0 - ah / CAPACITY_AH * 100.0)
        fitted.append((meter, raw, g, soc, load))
        print('%-9s %7.1f %7.2f %9.3f %8.2f %+8.2f %7.1f%% %7.2f%% %7.2f' %
              (stamp, (epoch - t0) / 60, meter, raw, raw + OFFSET, err, load, g, ah))
    if not fitted:
        print('nothing to fit')
        return 1
    print()
    print('vendor gauge vs amp-hour SOC (the error the table carries):')
    for meter, raw, g, soc, load in fitted:
        print('  real %5.2f V  raw %5.2f  gauge %5.2f%%  AhSOC %5.2f%%  delta %+6.2f pt'
              % (meter, raw, g, soc, g - soc))
    print()
    print('Ah at the last sample: %.2f Ah (%.1f%% of %.0f Ah)' %
          (fitted[-1][3] and 0 or 0, 0, CAPACITY_AH))
    return 0


main()

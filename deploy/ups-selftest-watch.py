#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Watch a running Megatec deep self-test and stop it on time or on voltage.

"TL" has no duration argument: on most firmware the UPS stays on the inverter
until the battery is low.  A 10 minute discharge therefore needs an external
deadline, and the low-voltage floor is the real safety net -- letting the pack
run to its own cutoff would drop the output and hard-power-off the host.

Both readings come from the native poller, which refreshes
/run/ups-poller/state.json every second (no vendor daemon, no shared memory):

  * battery_block (one 12 V block from the Q1 status line) x 2 = raw pack
    voltage, checked every PACK_SECONDS against the floor;
  * testing ("UPS Self-Test : Testing", decoded from status bit 0x04),
    checked every STATE_SECONDS.

Cancel goes through ups-selftest, which stops ups-poller (it owns interface
0), sends "CT" and starts ups-poller again.  The process exits once the test
is over, so it can be run as a transient systemd service:

    systemd-run --unit=ups-test-watch --collect \\
        /usr/local/sbin/ups-selftest-watch --until <epoch> --min-pack 22.2
"""

import argparse
import json
import sys
import time
from datetime import datetime

STATE = "/run/ups-poller/state.json"
SELFTEST = "/usr/local/sbin/ups-selftest"
DEFAULT_MIN_PACK = 22.2
DEFAULT_LOG = "/var/log/ups-selftest-watch.log"
PACK_MULTIPLIER = 2.0
# One missed publish is normal; the send window of a cancel stops the poller
# for ~15 s, so only call the link blind after well over that.
STALE_SECONDS = 25.0


def log(message):
    stamp = datetime.now().strftime("%H:%M:%S")
    line = "%s %s" % (stamp, message)
    print(line, flush=True)
    try:
        with open(DEFAULT_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_state():
    """Poller snapshot, or None when the file is missing or stale."""
    try:
        with open(STATE, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    try:
        age = time.time() - float(state.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    if age > STALE_SECONDS:
        log("poller state is %.0f s old, treating the link as blind" % age)
        return None
    return state


def pack_voltage(state):
    """Raw pack voltage (both 12 V blocks) from one poller frame, or None."""
    try:
        block = float(state.get("battery_block") or 0.0)
    except (TypeError, ValueError):
        return None
    return block * PACK_MULTIPLIER if block > 5.0 else None


def summary(state):
    try:
        pack = float(state.get("battery_block") or 0.0) * PACK_MULTIPLIER
    except (TypeError, ValueError):
        pack = 0.0
    return "self_test=%s ac=%s gauge=%s load=%s pack=%.2f output=%s" % (
        "Testing" if state.get("testing") else "Normal",
        "Bad" if state.get("ac_fail") else "Normal",
        state.get("gauge", "?"), state.get("load", "?"), pack,
        state.get("output_voltage", "?"))


def cancel(reason):
    import subprocess

    log("cancelling test: %s" % reason)
    for cmd in (
        ["systemctl", "stop", "ups-poller"],
        [SELFTEST, "cancel"],
        ["systemctl", "start", "ups-poller"],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            log("%s -> %d %s" % (" ".join(cmd), proc.returncode,
                                 (proc.stderr or proc.stdout).strip()[:160]))
        except (OSError, subprocess.TimeoutExpired) as exc:
            log("cancel step failed: %s %s" % (cmd, exc))
            return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", type=float, default=0.0,
                        help="epoch seconds at which the test is over (0 = no deadline)")
    parser.add_argument("--min-pack", type=float, default=DEFAULT_MIN_PACK,
                        help="raw pack voltage that forces an early cancel")
    parser.add_argument("--pack-interval", type=float, default=2.0)
    parser.add_argument("--state-interval", type=float, default=10.0)
    parser.add_argument("--max-runtime", type=int, default=1500,
                        help="hard stop for this watcher itself, seconds")
    args = parser.parse_args()

    end = args.until if args.until else time.time() + 600
    stop_at = time.time() + args.max_runtime
    log("watcher started, deadline %s, pack floor %.2f V" % (
        datetime.fromtimestamp(end).strftime("%H:%M:%S"), args.min_pack))

    next_pack = 0.0
    next_state = 0.0
    blind = 0
    lowest = None
    cancelled = False

    while time.time() < stop_at:
        now = time.time()
        if now >= next_pack:
            state = read_state()
            next_pack = now + args.pack_interval
            pack = pack_voltage(state) if state else None
            if pack is not None:
                if lowest is None or pack < lowest:
                    lowest = pack
                if not cancelled and pack <= args.min_pack:
                    cancel("pack voltage %.2f V at or below %.2f V floor"
                           % (pack, args.min_pack))
                    cancelled = True
        if now >= next_state:
            state = read_state()
            next_state = time.time() + args.state_interval
            if state is None:
                blind += 1
                if blind == 60 and not cancelled:
                    log("no fresh poller state for 60 reads: the link is blind, "
                        "the test state is unknown -- check ups-poller.service")
                time.sleep(1)
                continue
            blind = 0
            log(summary(state))
            if not state.get("testing"):
                if cancelled:
                    log("test confirmed stopped")
                    return 0
                if now >= end:
                    log("deadline passed and test already finished on its own")
                    return 0
            elif now >= end and not cancelled:
                cancel("requested duration reached at deadline")
                cancelled = True
        time.sleep(0.5)

    log("watcher hit its own %d s limit" % args.max_runtime)
    return 1


if __name__ == "__main__":
    sys.exit(main())

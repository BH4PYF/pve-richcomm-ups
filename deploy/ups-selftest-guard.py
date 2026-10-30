#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cancel a UPS self-test that is still running at its deadline.

The Megatec long test ("TL") has no duration argument -- on most firmware it
runs until the battery is low, so the guard is what keeps a requested 10
minutes from turning into a full discharge.  It reads the poller's decoded
screen snapshot (no second reader on the USB device) and sends the cancel
frame ("CT") through ups-selftest if the test is still going.

Run it as a one-shot timer, e.g.

    systemd-run --unit=ups-selftest-guard --on-active=780s \
        /usr/local/sbin/ups-selftest-guard

which cancels 13 minutes after the request went out.  Exiting without a
cancel is the normal case: the test finished on its own.
"""

import subprocess
import sys
import time

sys.path.insert(0, "/usr/local/lib/ups-pve")

import nut_bridge  # noqa: E402  (capture/parse of the vendor screen)

SELFTEST = "/usr/local/sbin/ups-selftest"
MAX_WAIT_SECONDS = 60


def self_test_running():
    try:
        values = nut_bridge.parse(nut_bridge.capture())
    except Exception as exc:
        print("screen read failed: %s" % exc, flush=True)
        return None
    state = values.get("self_test", "")
    print("vendor screen self-test field: %s" % (state or "?"), flush=True)
    return state.lower() == "testing"


def cancel():
    for cmd in (
        ["systemctl", "stop", "ups-poller"],
        [SELFTEST, "cancel"],
        ["systemctl", "start", "ups-poller"],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print("cancel step failed: %s %s" % (cmd, exc), flush=True)
            return False
        print("%s -> %d %s" % (" ".join(cmd), proc.returncode,
                               (proc.stderr or proc.stdout).strip()[:200]),
              flush=True)
    return True


def main():
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        running = self_test_running()
        if running is None:
            # The poller may be mid-restart; retry until the read works.
            time.sleep(5)
            continue
        if not running:
            print("self-test already finished, nothing to cancel", flush=True)
            return 0
        print("self-test still running at the deadline, cancelling", flush=True)
        return 0 if cancel() else 1
    print("could not read the poller screen in time", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feed Richcomm Power Manager readings into NUT's dummy-ups driver.

The vendor program (ups_monitor, legacy i386) keeps live values in SysV
shared memory and ups_status prints them on a terminal.  This daemon
captures one status frame through a pty every POLL_SECONDS and atomically
rewrites /run/ups-bridge/ups.dev.  dummy-ups runs in dummy-once mode and
re-reads that file whenever its mtime changes.

If /run/ups-bridge/fsd exists (touched by shutdown.sh when the vendor
program starts a system shutdown), FSD is added to ups.status so the NAS
shuts down too instead of waiting for the battery to die.
"""

import ctypes
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import tempfile
import time

VENDOR_DIR = "/opt/ups-manager"
STATUS_BIN = "./ups_status"
OUT_DIR = "/run/ups-bridge"
OUT_FILE = os.path.join(OUT_DIR, "ups.dev")
FSD_FLAG = os.path.join(OUT_DIR, "fsd")
# Status row in the PVE node summary: pveproxy serves *.js from here.
WEB_STATUS_FILE = "/usr/share/pve-manager/js/ups-status.js"
RUNTIME_CONF = "/etc/ups-pve/runtime.conf"
SHM_KEY = 0x1B56
SHM_SIZE = 224
SHM_BATTERY_VOLTAGE_OFFSET = 140
# Native usbfs poller (ups-poller.service) replaces the vendor daemon.  Its
# state is authoritative when the timestamp is fresh; otherwise fall back to
# the vendor shm/screen path so a rollback only needs the service swap.
POLLER_STATE = "/run/ups-poller/state.json"
POLLER_SCREEN = "/run/ups-poller/screen.txt"
POLLER_STALE = 15.0
# The pack is 2 x 12 V in series; the vendor program reports one 12 V block.
BATTERY_PACK_MULTIPLIER = 2.0
VOLTAGE_DERATE_START = 23.2
VOLTAGE_CUTOFF = 21.0
STATE_DIR = "/var/lib/ups-pve"
STATE_PATH = os.path.join(STATE_DIR, "battery.json")
MAX_STATE_GAP = 120.0
LB_SOC = 20.0
LB_VOLTAGE = 21.6
POLL_SECONDS = 10
TICK_SECONDS = 2
CAPTURE_SECONDS = 2.5
# Link supervision: the vendor frame keeps looking fresh after the USB device
# disappears, so check the device and the vendor daemon directly.  Only flag
# the outage after three failed checks so a self-test (service stopped for
# ~15 s) does not raise ALARM.
USB_VID = "0925"
USB_PID = "1234"
USB_SYSFS = "/sys/bus/usb/devices"
MONITOR_PID = os.path.join(VENDOR_DIR, "Monitor.pid")
TIMECFG = os.path.join(VENDOR_DIR, "TimeCfg.conf")
COMM_GRACE = 3
# About 30 minutes of history: the capture loop runs every ~12 s because the
# vendor status screen itself takes a couple of seconds to read.
HISTORY_POINTS = 144

NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
PATTERNS = {
    "input_voltage": re.compile(r"UPS Input Voltage\s*:\s*" + NUMBER),
    "output_voltage": re.compile(r"UPS Output Voltage\s*:\s*" + NUMBER),
    "line_frequency": re.compile(r"UPS Line Frequency\s*:\s*" + NUMBER),
    "input_frequency": re.compile(r"UPS Input Frequency\s*:\s*" + NUMBER),
    "load": re.compile(r"UPS Power Loading\s*:\s*" + NUMBER),
    "temperature": re.compile(r"UPS Temperature\s*:\s*" + NUMBER),
    "charge": re.compile(r"UPS Battery Level\s*:\s*" + NUMBER),
    "ac_power": re.compile(r"Input AC Power\s*:\s*([A-Za-z]+)"),
    "battery_status": re.compile(r"Battery Status\s*:\s*([A-Za-z]+)"),
    "boost_buck": re.compile(r"Boost/Buck\s*:\s*([A-Za-z]+)"),
    # The screen also shows the self-test state, which shared memory does not
    # expose.  During a self-test the UPS runs its load from the inverter even
    # though "Input AC Power" stays Normal, so the field is needed to keep the
    # battery model honest.
    "self_test": re.compile(r"UPS Self-Test\s*:\s*([A-Za-z]+)"),
}

_libc = None
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
    _libc.shmget.restype = ctypes.c_int
    _libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    _libc.shmat.restype = ctypes.c_void_p
    _libc.shmdt.argtypes = [ctypes.c_void_p]
    _libc.shmdt.restype = ctypes.c_int
except OSError:
    _libc = None


def poller_state():
    """Last state written by ups-poller, or None when it has never run."""
    try:
        with open(POLLER_STATE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def poller_fresh(seconds=POLLER_STALE):
    """True when ups-poller produced a status line recently enough to trust."""
    state = poller_state()
    if not state:
        return False
    try:
        return time.time() - float(state.get("ts") or 0) <= seconds
    except (TypeError, ValueError):
        return False


def read_battery_voltage():
    """Read pack voltage from the vendor program's SysV shm.

    The vendor firmware reports one 12 V block of the 24 V series pack, so
    the returned value is scaled up to the full pack voltage for NUT.
    """
    state = poller_state()
    if state and poller_fresh() and state.get("battery_block"):
        try:
            return float(state["battery_block"]) * BATTERY_PACK_MULTIPLIER
        except (TypeError, ValueError):
            pass
    if _libc is None:
        return None
    shmid = _libc.shmget(SHM_KEY, 0, 0)
    if shmid < 0:
        return None
    address = _libc.shmat(shmid, None, 0)
    if not address or address == ctypes.c_void_p(-1).value:
        return None
    try:
        raw = ctypes.string_at(address, SHM_SIZE)
    finally:
        _libc.shmdt(address)
    value = struct.unpack_from("<f", raw, SHM_BATTERY_VOLTAGE_OFFSET)[0]
    if 0.0 < value < 100.0:
        return value * BATTERY_PACK_MULTIPLIER
    return None


def shm_has_data():
    """True when the vendor daemon has actually written a live status block.

    A USB re-plug can leave /dev/usb/hiddevN behind with a dev_t the kernel no
    longer serves, so ups_monitor opens ENODEV on every index, keeps running and
    leaves shared memory zeroed.  lsusb still shows the UPS and the daemon is
    still alive, so presence plus liveness are not enough: without this check
    the bridge happily publishes "OL" with input 0.0 V and a battery voltage of
    0.0 V, which is stale data wearing a fresh timestamp.
    """
    if _libc is None:
        return True
    if os.path.exists(POLLER_STATE):
        # Native poller owns the link now; its timestamp is the liveness test.
        return poller_fresh()
    shmid = _libc.shmget(SHM_KEY, 0, 0)
    if shmid < 0:
        return False
    address = _libc.shmat(shmid, None, 0)
    if not address or address == ctypes.c_void_p(-1).value:
        return False
    try:
        raw = ctypes.string_at(address, SHM_SIZE)
    finally:
        _libc.shmdt(address)
    block = struct.unpack_from("<f", raw, SHM_BATTERY_VOLTAGE_OFFSET)[0]
    return block > 5.0


def read_acfail_delay():
    """AC-fail shutdown delay (seconds) configured in the vendor program.

    TimeCfg.conf is a 52-byte binary blob: 4-byte magic, then the AC-fail,
    battery-low, UPS-bad and warning-interval delays as little-endian int32.
    -1 means the vendor cancels the shutdown, which is reported as 0 so NUT
    clients do not advertise a delay that never fires.
    """
    try:
        with open(TIMECFG, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if len(raw) < 20:
        return None
    value = struct.unpack_from("<i", raw, 4)[0]
    return 0 if value < 0 else value


def load_runtime_conf():
    conf = {
        "RUNTIME_MINUTES": "0",
        "REFERENCE_LOAD_PCT": "22",
        "MIN_LOAD_PCT": "5",
        "RATED_WATTS": "400",
        "CHARGE_HOURS": "10",
        "FULL_VOLTAGE": "26.7",
        "VOLTAGE_OFFSET": "0",
    }
    try:
        with open(RUNTIME_CONF, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip()
    except OSError:
        pass
    return conf


def apply_thresholds(conf):
    """Take the voltage decision thresholds from runtime.conf when present.

    The 37-minute discharge on 2026-10-29 showed the vendor ADC sitting dead
    flat on 23.60 V (raw pack) from minute 8 to minute 30 while the meter fell
    24.4 -> 24.2 V, i.e. the loaded plateau at ~93 % state of charge is only
    0.4 V above the old VOLTAGE_DERATE_START of 23.2 V.  Any real outage would
    therefore start derating the runtime estimate almost immediately.  The
    plateau is now known, so the threshold moves to 22.6 V raw (23.4 V on the
    meter) -- a full volt below the plateau, roughly the last third of the
    pack at this load.
    """
    global VOLTAGE_DERATE_START, VOLTAGE_CUTOFF, LB_VOLTAGE, LB_SOC
    for key in ("VOLTAGE_DERATE_START", "VOLTAGE_CUTOFF", "LB_VOLTAGE", "LB_SOC"):
        try:
            globals()[key] = float(conf[key])
        except (KeyError, TypeError, ValueError):
            continue


def voltage_offset(conf):
    """Display-only correction for the vendor's under-reading pack voltage.

    The meter read 26.3 V while the vendor reported ~25.45 V, so the offset
    is added to NUT's battery.voltage, the web card and its history only.
    SOC, runtime derating and the LB threshold keep using the raw vendor
    value so the behaviour validated by the short outage test is unchanged.
    """
    try:
        return float(conf.get("VOLTAGE_OFFSET", "0"))
    except (TypeError, ValueError):
        return 0.0


def runtime_inputs(values):
    """Extract load % from one parsed status frame."""
    try:
        load = float(values.get("load", ""))
    except (TypeError, ValueError):
        load = None
    return load


def usable_wh(conf):
    """Usable AC energy implied by the calibrated runtime reference point."""
    try:
        minutes = float(conf.get("RUNTIME_MINUTES", "0"))
        reference = float(conf.get("REFERENCE_LOAD_PCT", "22"))
        watts = float(conf.get("RATED_WATTS", "400"))
    except (TypeError, ValueError):
        return None
    if minutes <= 0 or reference <= 0 or watts <= 0:
        return None
    return minutes / 60.0 * (reference / 100.0 * watts)


def power_watts(conf, load):
    """Estimate AC output power from the UPS load percentage."""
    try:
        rated = float(conf.get("RATED_WATTS", "400"))
        minimum = float(conf.get("MIN_LOAD_PCT", "5"))
    except (TypeError, ValueError):
        return None
    if load is None or load <= 0 or rated <= 0:
        return None
    return max(load, minimum) / 100.0 * rated


def load_battery_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {"soc": float(data["soc"]), "last_ts": float(data["last_ts"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_battery_state(soc, when):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    fd, path = tempfile.mkstemp(dir=STATE_DIR, prefix=".battery.")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"soc": round(soc, 2), "last_ts": when}, handle)
        os.chmod(path, 0o600)
        os.replace(path, STATE_PATH)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def update_soc(conf, state, ac_normal, voltage, load, vendor_charge, now):
    """Track state of charge by integrating load power during outages.

    The vendor percentage is a load-uncompensated voltage table, so it is
    only used to seed the model when no state exists.  On mains the pack is
    recharged at a configurable rate and only reaches 100 % at float voltage.
    """
    if state is None:
        try:
            full_v = float(conf.get("FULL_VOLTAGE", "26.7"))
        except (TypeError, ValueError):
            full_v = 26.7
        if ac_normal and voltage is not None and voltage >= full_v:
            soc = 100.0
        elif vendor_charge is not None:
            soc = max(1.0, min(99.0, vendor_charge))
        else:
            soc = 100.0
        dt = 0.0
    else:
        soc = state["soc"]
        dt = max(0.0, min(now - state["last_ts"], MAX_STATE_GAP))

    if ac_normal:
        try:
            hours = float(conf.get("CHARGE_HOURS", "10"))
        except (TypeError, ValueError):
            hours = 10.0
        if hours <= 0:
            hours = 10.0
        soc = min(100.0, soc + 100.0 / (hours * 3600.0) * dt)
        try:
            full_v = float(conf.get("FULL_VOLTAGE", "26.7"))
        except (TypeError, ValueError):
            full_v = 26.7
        if voltage is not None and voltage >= full_v:
            if soc >= 99.5:
                soc = 100.0
        else:
            soc = min(soc, 99.0)
    else:
        energy = usable_wh(conf)
        power = power_watts(conf, load)
        if energy and power is not None and dt > 0:
            soc -= power * dt / 3600.0 / energy * 100.0
    return max(0.0, min(100.0, soc))


def compute_runtime(conf, load, voltage, soc):
    """Estimate remaining runtime (seconds) from capacity, load and SOC."""
    try:
        minutes = float(conf.get("RUNTIME_MINUTES", "0"))
        reference = float(conf.get("REFERENCE_LOAD_PCT", "22"))
        minimum = float(conf.get("MIN_LOAD_PCT", "5"))
    except (TypeError, ValueError):
        return None
    if minutes <= 0 or reference <= 0 or load is None or load <= 0:
        return None
    factor = reference / max(load, minimum)
    seconds = minutes * 60.0 * factor * (max(0.0, min(100.0, soc)) / 100.0)
    if voltage is not None and voltage < VOLTAGE_DERATE_START:
        derate = (voltage - VOLTAGE_CUTOFF) / (VOLTAGE_DERATE_START - VOLTAGE_CUTOFF)
        seconds *= max(0.1, min(1.0, derate))
    return max(60, min(int(seconds), 86400))


def capture():
    """Run the vendor status client and return one full screen frame."""
    state = poller_state()
    if state and poller_fresh():
        try:
            with open(POLLER_SCREEN, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            pass
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(VENDOR_DIR)
            os.environ["TERM"] = "dumb"
            os.environ["LANG"] = "C"
            os.execv(STATUS_BIN, [STATUS_BIN])
        except Exception:
            pass
        os._exit(127)

    chunks = []
    deadline = time.time() + CAPTURE_SECONDS
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    text = b"".join(chunks).decode("latin1")
    return text.replace("\r\n", "\n").replace("\r", "")


def parse(text):
    values = {}
    for key, pattern in PATTERNS.items():
        match = pattern.search(text)
        if match:
            values[key] = match.group(1)
    return values


def usb_present():
    """True while the UPS USB device is enumerated on this host."""
    try:
        names = os.listdir(USB_SYSFS)
    except OSError:
        return False
    for name in names:
        path = os.path.join(USB_SYSFS, name)
        try:
            with open(os.path.join(path, "idVendor")) as handle:
                vid = handle.read().strip()
            with open(os.path.join(path, "idProduct")) as handle:
                pid = handle.read().strip()
        except OSError:
            continue
        if vid == USB_VID and pid == USB_PID:
            return True
    return False


def vendor_running():
    """True while the UPS data source (poller or vendor ups_monitor) is alive."""
    if os.path.exists(POLLER_STATE):
        return poller_fresh()
    try:
        with open(MONITOR_PID) as handle:
            pid = int(handle.read().strip())
        with open("/proc/%d/comm" % pid) as handle:
            return handle.read().strip() == "ups_monitor"
    except (OSError, ValueError):
        return False


def build_definition(values, battery_voltage=None, runtime_seconds=None, charge=None, low_battery=False, comm_lost=False, testing=False, acfail_delay=None, vendor_charge=None):
    ac = values.get("ac_power", "").lower()
    battery = values.get("battery_status", "").lower()
    # A self-test leaves the mains connected but carries the load from the
    # inverter, so far as the battery is concerned it is the same situation.
    on_battery = ac != "normal" or testing

    status = []
    if values:
        status.append("OL" if ac == "normal" else "OB")
        if on_battery and (battery not in ("normal", "ok") or low_battery):
            status.append("LB")
    bb = values.get("boost_buck", "").lower()
    if bb == "buck":
        status.append("TRIM")
    elif bb == "boost":
        status.append("BOOST")
    if os.path.exists(FSD_FLAG):
        # shutdown.sh only touches the flag once the vendor program has
        # decided to power the host off, in any AC state.
        status.append("FSD")
    if testing:
        # NUT's standard "UPS is doing a self-test" token.  The mains stays
        # present, but the load runs from the inverter, so clients should know.
        status.append("TEST")
    if comm_lost:
        status.append("ALARM")

    lines = [
        "device.mfr: Richcomm",
        "device.model: UPS USB MON V1.4",
        "device.type: ups",
        "ups.mfr: Richcomm",
        "ups.model: UPS USB MON V1.4",
        "ups.status: " + " ".join(status),
        "battery.charge.low: 20",
        "battery.voltage.nominal: 24",
        "ups.delay.shutdown: %d" % (acfail_delay if acfail_delay is not None else 3600),
    ]
    if comm_lost:
        lines.append("ups.alarm: USB communication lost")
    if charge is not None:
        lines.append("battery.charge: %d" % int(round(charge)))
    if vendor_charge is not None:
        # The vendor screen percent is not a capacity: it is a lookup on the
        # single 12 V block voltage, charge = round((V_block - 10) * 10) / 36,
        # so one 0.1 V step is 2.78 % and the whole 0-100 % range is squeezed
        # into 20.0 -> 27.2 V of pack voltage.  Measured 2026-10-29: it read
        # 50 % flat for 23 minutes while the pack was still at ~93 %.
        # Published only so the two numbers can be compared side by side.
        lines.append("battery.charge.vendor: %d" % int(round(vendor_charge)))
    if "input_voltage" in values:
        lines.append("input.voltage: %.1f" % float(values["input_voltage"]))
    if "output_voltage" in values:
        lines.append("output.voltage: %.1f" % float(values["output_voltage"]))
    if "input_frequency" in values:
        lines.append("input.frequency: %.1f" % float(values["input_frequency"]))
    if "line_frequency" in values:
        lines.append("output.frequency: %.1f" % float(values["line_frequency"]))
    if "load" in values:
        lines.append("ups.load: %d" % int(round(float(values["load"]))))
    if "temperature" in values:
        lines.append("ups.temperature: %.1f" % float(values["temperature"]))
    if battery_voltage is not None:
        lines.append("battery.voltage: %.2f" % battery_voltage)
    if runtime_seconds is not None:
        lines.append("battery.runtime: %d" % runtime_seconds)
    return "\n".join(lines) + "\n"


def write_atomic(text):
    fd, path = tempfile.mkstemp(dir=OUT_DIR, prefix=".ups.dev.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(path, 0o644)
        os.replace(path, OUT_FILE)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def write_web_status(
    status_text,
    values,
    charge,
    runtime,
    voltage,
    now,
    history=None,
    warn_voltage=None,
    stale=False,
    self_test=False,
):
    """Publish one JSONP frame for the UPS row in the PVE web UI."""
    payload = {
        "ts": int(now),
        "status": status_text,
        "stale": bool(stale),
        "self_test": bool(self_test),
        "charge": None if charge is None else int(round(charge)),
        "runtime": None if runtime is None else int(runtime),
        "voltage": None if voltage is None else round(voltage, 2),
        "warn_voltage": None if warn_voltage is None else round(warn_voltage, 2),
        "history": history or [],
    }
    for source in ("load", "input_voltage", "output_voltage", "temperature"):
        if source in values:
            try:
                payload[source] = round(float(values[source]), 1)
            except ValueError:
                pass

    text = "window.__PVE_UPS_STATUS__&&window.__PVE_UPS_STATUS__(%s);\n" % json.dumps(
        payload, separators=(",", ":")
    )
    directory = os.path.dirname(WEB_STATUS_FILE)
    fd, path = tempfile.mkstemp(dir=directory, prefix=".ups-status.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(path, 0o644)
        os.replace(path, WEB_STATUS_FILE)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def device_has_alarm():
    """True when the published device file still carries an alarm line."""
    try:
        with open(OUT_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("ups.alarm:"):
                    return True
    except OSError:
        pass
    return False


def restart_driver():
    """Make dummy-ups forget the stale alarm after a comm-loss episode.

    dummy-ups never deletes a variable that disappears from the device file,
    and an empty value is not accepted either (the driver reuses the previous
    value), so the only way to drop ups.alarm is a driver restart, which
    re-reads the alarm-free file.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "restart", "nut-driver@ups"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("driver restart failed: %s" % exc, flush=True)
        return
    if proc.returncode != 0:
        print(
            "driver restart failed: %s"
            % (proc.stderr or proc.stdout).strip()[:200],
            flush=True,
        )


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        os.chmod(OUT_DIR, 0o755)
    except OSError:
        pass

    battery_state = load_battery_state()
    last_values = None
    last_state = None
    last_flag = False
    last_voltage = None
    last_offset = 0.0
    last_delay = None
    last_runtime = None
    last_charge = None
    last_vendor_charge = None
    last_low = False
    last_testing = False
    next_capture = 0.0
    web_error = None
    history = []
    comm_lost = False
    comm_fail = 0
    # Set when the driver may still hold ups.alarm from an earlier episode.
    alarm_written = device_has_alarm()
    while True:
        flag = os.path.exists(FSD_FLAG)
        now = time.time()
        try:
            write_needed = False
            if now >= next_capture:
                link_ok = usb_present() and vendor_running()
                if link_ok and not shm_has_data():
                    link_ok = False
                if link_ok:
                    comm_fail = 0
                    if comm_lost:
                        comm_lost = False
                        write_needed = True
                        print("usb link restored", flush=True)
                else:
                    comm_fail += 1
                    if not comm_lost and comm_fail >= COMM_GRACE:
                        comm_lost = True
                        write_needed = True
                        print(
                            "vendor data invalid (device %s) -- "
                            "battery model frozen"
                            % ("present" if usb_present() else "absent"),
                            flush=True,
                        )
                    next_capture = now + POLL_SECONDS

                if link_ok:
                    values = parse(capture())
                    if "input_voltage" not in values and "charge" not in values:
                        raise RuntimeError("no readings in status frame")

                    ac_normal = values.get("ac_power", "").lower() == "normal"
                    # A self-test keeps "Input AC Power: Normal" but runs the
                    # load from the inverter, so the battery really drains.
                    testing = values.get("self_test", "").lower() == "testing"
                    if testing != last_testing:
                        print(
                            "self-test started (AC still reported normal)"
                            if testing
                            else "self-test finished",
                            flush=True,
                        )
                        last_testing = testing

                    if ac_normal and flag and not testing:
                        try:
                            os.unlink(FSD_FLAG)
                        except OSError:
                            pass
                        flag = False

                    voltage = read_battery_voltage()
                    if voltage is not None:
                        last_voltage = voltage

                    load = runtime_inputs(values)
                    vendor_charge = None
                    if "charge" in values:
                        try:
                            vendor_charge = float(values["charge"])
                        except ValueError:
                            pass

                    conf = load_runtime_conf()
                    apply_thresholds(conf)
                    last_offset = voltage_offset(conf)
                    last_delay = read_acfail_delay()
                    last_charge = update_soc(
                        conf,
                        battery_state,
                        # Charging is impossible while the inverter carries the
                        # load, even though the mains is present in a self-test.
                        ac_normal and not testing,
                        last_voltage,
                        load,
                        vendor_charge,
                        now,
                    )
                    battery_state = {"soc": last_charge, "last_ts": now}
                    last_vendor_charge = vendor_charge
                    try:
                        save_battery_state(last_charge, now)
                    except Exception as exc:
                        print("state save failed: %s" % exc, flush=True)

                    last_low = last_charge <= LB_SOC or (
                        last_voltage is not None and last_voltage < LB_VOLTAGE
                    )
                    last_runtime = compute_runtime(
                        conf, load, last_voltage, last_charge
                    )

                    last_values = values
                    display_voltage = (
                        None if last_voltage is None else last_voltage + last_offset
                    )
                    history.append(
                        [
                            int(now),
                            None
                            if display_voltage is None
                            else round(display_voltage, 2),
                            None if load is None else round(load, 1),
                        ]
                    )
                    if len(history) > HISTORY_POINTS:
                        del history[:-HISTORY_POINTS]
                    next_capture = time.time() + POLL_SECONDS
                    write_needed = True
            elif flag != last_flag and last_values is not None:
                # The shutdown flag appeared/disappeared between captures:
                # republish straight away so NUT clients see FSD in ~2 s.
                write_needed = True

            if write_needed and (last_values is not None or comm_lost):
                display_voltage = (
                    None if last_voltage is None else last_voltage + last_offset
                )
                definition = build_definition(
                    last_values or {},
                    display_voltage,
                    last_runtime,
                    last_charge,
                    last_low,
                    comm_lost,
                    last_testing,
                    last_delay,
                    last_vendor_charge,
                )
                write_atomic(definition)
                last_flag = flag

                if comm_lost:
                    alarm_written = True
                elif alarm_written:
                    # The previous frame carried an alarm; the driver keeps
                    # that variable until it re-reads the file.  Restart it
                    # once now that the alarm-free frame is in place.
                    alarm_written = False
                    restart_driver()

                state = definition.split("ups.status: ", 1)[1].split("\n", 1)[0]
                if state != last_state:
                    if display_voltage is None:
                        voltage_text = "?"
                    elif last_offset:
                        voltage_text = "%.2f (raw %.2f)" % (
                            display_voltage,
                            last_voltage,
                        )
                    else:
                        voltage_text = "%.2f" % display_voltage
                    charge_text = "?" if last_charge is None else "%.0f" % last_charge
                    print(
                        "ups.status: %s (battery %s%%, pack %s V)"
                        % (state, charge_text, voltage_text),
                        flush=True,
                    )
                    last_state = state

                try:
                    write_web_status(
                        state,
                        last_values or {},
                        last_charge,
                        last_runtime,
                        display_voltage,
                        now,
                        history,
                        VOLTAGE_DERATE_START + last_offset,
                        comm_lost,
                        last_testing,
                    )
                    web_error = None
                except Exception as exc:
                    message = "web status write failed: %s" % exc
                    if message != web_error:
                        print(message, flush=True)
                        web_error = message
        except Exception as exc:
            print("poll failed: %s" % exc, flush=True)
            next_capture = time.time() + POLL_SECONDS

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()

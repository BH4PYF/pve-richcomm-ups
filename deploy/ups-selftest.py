#!/usr/bin/env python3
"""Trigger a UPS battery self-test using the vendor HID frames.

Wire frame (usbmon, SET_REPORT bmRequestType=0x21 bRequest=0x09
wValue=0x0200 wIndex=0):
    A2 54 0D          'T'   + CR -> quick test, fixed 10 seconds (verified)
    A3 54 4C 0D       'TL'  + CR -> deep test (Megatec long test)
    A3 43 54 0D       'CT'  + CR -> cancel a running test
    A3 54 31 30       'T10' no CR -> timed test, 10 minutes (verified in VM)

The first byte is 0xA0 | number of ASCII bytes that follow, so the transfer
length is that count plus one.  These letters are the Megatec/Q1 family that
the vendor program speaks: ups_monitor itself only sends "Q1", "I", "F" and
"S%02d", and NUT's blazer driver (drivers/blazer.c) publishes exactly
test.battery.start.quick "T", test.battery.start.deep "TL" and
test.battery.stop "CT" for it.

A timed test exists, but the terminator decides whether the UPS takes it.
On 2026-10-30 the Windows vendor program (IpPowerSE in the Win10 VM) sent
"A3 54 31 30" -- 'T10', three ASCII bytes, NO CR, 4-byte transfer -- at the
next Q1 slot, and the UPS ran a real 10 minute battery test (input opened,
self-test field active, gauge 72 -> 58 %, screen countdown).  Our earlier
"A4 54 31 30 0D" (same letters plus the CR used by T/TL/CT) was acked and
ignored: screen stayed "Self-Test : Normal", pack stayed flat.  So 'timed'
goes out without the terminator; T/TL/CT keep theirs.  The alternative for a
wanted duration is "deep" plus ups-selftest-watch, which sends "CT" when the
clock or a voltage floor says stop.

A test moves the load onto the inverter.  With "T" mains stays present and the
screen keeps printing "Input AC Power: Normal"; with "TL" the UPS opens its
input after a few seconds, so the screen reads "Input AC Power: Bad", input
voltage 0 and the warning word gains 0x80.  The bridge maps both to OB, and
TEST while the screen field says Testing.  Run only while the UPS reports OL --
never send a test while the site is already on battery.  ups-poller owns
interface 0 through usbfs, so ups-poller.service has to be stopped for the
send window and started again afterwards; the test itself keeps running on the
UPS after this program exits.

The UPS streams 6-byte status frames on interrupt endpoint 0x81. IpPowerSE
keeps reading them while it sends the test command, so this tool does the
same: a reader thread drains the endpoint while the SET_REPORT goes out.
"""

import ctypes as C
import sys
import threading
import time

VID, PID = 0x0925, 0x1234
REPORT_PREFIX = 0xA0
TERMINATOR = 0x0D
EP_IN = 0x81
WATCH_SECONDS = 12

# IpPowerSE always opens the device with SET_IDLE, a 0x80 0xd0 handshake and
# one pass of I / F / Q1 before any control command; replay that verbatim.
PRELUDE = [(0x21, 0x0A, 0x0000, 0, b""),
           (0x21, 0x09, 0x0200, 0, bytes.fromhex("80d00000")),
           (0x21, 0x09, 0x0200, 0, bytes.fromhex("a2490d00")),
           (0x21, 0x09, 0x0200, 0, bytes.fromhex("a2460d00")),
           (0x21, 0x09, 0x0200, 0, bytes.fromhex("a351310d"))]


def frame(text, terminator=True):
    """One SET_REPORT payload: 0xA0|len, ASCII, and a CR unless 'timed'.

    Verified on the wire: 'T' (a2 54 0d), 'Q1' (a3 51 31 0d), and the timed
    form 'T10' without a terminator (a3 54 31 30).
    """
    data = text.encode("ascii") + (bytes([TERMINATOR]) if terminator else b"")
    return bytes([REPORT_PREFIX | len(data)]) + data


class DevDesc(C.Structure):
    _fields_ = [
        ("bLength", C.c_uint8), ("bDescriptorType", C.c_uint8),
        ("bcdUSB", C.c_uint16), ("bDeviceClass", C.c_uint8),
        ("bDeviceSubClass", C.c_uint8), ("bDeviceProtocol", C.c_uint8),
        ("bMaxPacketSize0", C.c_uint8), ("idVendor", C.c_uint16),
        ("idProduct", C.c_uint16), ("bcdDevice", C.c_uint16),
        ("iManufacturer", C.c_uint8), ("iProduct", C.c_uint8),
        ("iSerialNumber", C.c_uint8), ("bNumConfigurations", C.c_uint8),
    ]


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "test"
    commands = {"test": "T", "deep": "TL", "cancel": "CT"}
    if action == "timed":
        try:
            minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= 99:
            print(f"usage: {sys.argv[0]} timed <minutes 1-99>", file=sys.stderr)
            return 2
        # Only "T10" has been verified on this unit; other lengths follow the
        # vendor's own two-digit format but are untested.
        payload = frame("T%02d" % minutes, terminator=False)
    elif action in commands:
        payload = frame(commands[action])
    else:
        print(f"usage: {sys.argv[0]} [test|deep|cancel|timed <minutes>]",
              file=sys.stderr)
        return 2

    lib = C.CDLL("libusb-1.0.so.0")
    lib.libusb_init.argtypes = [C.POINTER(C.c_void_p)]
    lib.libusb_init.restype = C.c_int
    lib.libusb_get_device_list.argtypes = [C.c_void_p,
                                           C.POINTER(C.POINTER(C.c_void_p))]
    lib.libusb_get_device_list.restype = C.c_ssize_t
    lib.libusb_get_device_descriptor.argtypes = [C.c_void_p,
                                                 C.POINTER(DevDesc)]
    lib.libusb_open.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
    lib.libusb_detach_kernel_driver.argtypes = [C.c_void_p, C.c_int]
    lib.libusb_attach_kernel_driver.argtypes = [C.c_void_p, C.c_int]
    lib.libusb_claim_interface.argtypes = [C.c_void_p, C.c_int]
    lib.libusb_control_transfer.argtypes = [
        C.c_void_p, C.c_uint8, C.c_uint8, C.c_uint16, C.c_uint16,
        C.POINTER(C.c_ubyte), C.c_uint16, C.c_uint]
    lib.libusb_control_transfer.restype = C.c_int
    lib.libusb_interrupt_transfer.argtypes = [
        C.c_void_p, C.c_ubyte, C.POINTER(C.c_ubyte), C.c_int,
        C.POINTER(C.c_int), C.c_uint]
    lib.libusb_interrupt_transfer.restype = C.c_int
    lib.libusb_release_interface.argtypes = [C.c_void_p, C.c_int]
    lib.libusb_close.argtypes = [C.c_void_p]
    lib.libusb_free_device_list.argtypes = [C.POINTER(C.c_void_p), C.c_int]
    lib.libusb_exit.argtypes = [C.c_void_p]

    ctx = C.c_void_p()
    if lib.libusb_init(C.byref(ctx)) != 0:
        print("libusb_init failed", file=sys.stderr)
        return 1

    devs = C.POINTER(C.c_void_p)()
    n = lib.libusb_get_device_list(ctx, C.byref(devs))
    handle = C.c_void_p()
    for i in range(n):
        desc = DevDesc()
        lib.libusb_get_device_descriptor(devs[i], C.byref(desc))
        if desc.idVendor == VID and desc.idProduct == PID:
            rc = lib.libusb_open(devs[i], C.byref(handle))
            if rc != 0:
                print(f"libusb_open failed: {rc}", file=sys.stderr)
                handle = C.c_void_p()
            break

    if not handle:
        print("UPS 0925:1234 not found (or open failed)", file=sys.stderr)
        return 1

    lib.libusb_free_device_list(devs, 1)

    # usbhid owns interface 0 unless the vendor daemon kicked it; detach so
    # libusb can claim it. Reattach afterwards so hidraw keeps working.
    detached = lib.libusb_detach_kernel_driver(handle, 0) == 0
    rc = lib.libusb_claim_interface(handle, 0)
    if rc != 0:
        print(f"claim interface failed: {rc} "
              "(stop ups-poller.service first)", file=sys.stderr)
        if detached:
            lib.libusb_attach_kernel_driver(handle, 0)
        lib.libusb_close(handle)
        lib.libusb_exit(ctx)
        return 1

    stop = threading.Event()
    frames = []

    def reader():
        buf = (C.c_ubyte * 64)()
        got = C.c_int(0)
        while not stop.is_set():
            rc = lib.libusb_interrupt_transfer(handle, EP_IN, buf, 6,
                                               C.byref(got), 100)
            if rc == 0 and got.value:
                frames.append((time.time(),
                               bytes(buf[:got.value])))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.3)

    def send(request_type, request, value, index, data, timeout=1000):
        n = len(data)
        buf = (C.c_ubyte * max(n, 1))(*data) if n else None
        return lib.libusb_control_transfer(handle, request_type, request,
                                           value, index, buf, n, timeout)

    for request_type, request, value, index, data in PRELUDE:
        rc = send(request_type, request, value, index, data)
        print(f"prelude {data.hex() if data else f'setup {request:02x}'}: {rc}")
        time.sleep(0.2)

    # IpPowerSE sends T where the next Q1 poll would go, ~1.5 s after the
    # previous Q1. Firing it earlier lands in the outgoing status response
    # and the UPS drops the command.
    time.sleep(1.5)

    rc = send(0x21, 0x09, 0x0200, 0, bytes(payload))
    sent_at = time.time()
    print(f"{action}: sent {' '.join(f'{b:02x}' for b in payload)} "
          f"-> {rc} bytes @ {time.strftime('%H:%M:%S')}")
    ok = rc == len(payload)

    time.sleep(WATCH_SECONDS)
    stop.set()
    thread.join(timeout=1)

    distinct = {}
    for ts, f in frames:
        distinct[f] = distinct.get(f, 0) + 1
    if frames:
        print(
            f"status frames: {len(frames)} in {WATCH_SECONDS}s, "
            f"{len(distinct)} distinct "
            f"(command went out at t+{sent_at - frames[0][0]:.1f}s)"
        )
    else:
        print("status frames: none")
    for f, count in sorted(distinct.items(), key=lambda kv: -kv[1])[:6]:
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in f)
        print(f"  {f.hex()}  {printable:<8} x{count}")

    lib.libusb_release_interface(handle, 0)
    if detached:
        lib.libusb_attach_kernel_driver(handle, 0)
    lib.libusb_close(handle)
    lib.libusb_exit(ctx)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

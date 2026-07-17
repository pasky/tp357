#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import sys
import time

from gi.repository import GLib
import pydbus

# Hard cap on how long we wait for a device's notifications. Without this the
# GLib mainloop blocks forever if the terminating packet never arrives (flaky
# BLE, half-open connection, ...). A hung process keeps holding adapter
# resources (connection/notify/discovery sessions); enough of them accumulating
# eventually wedges the controller so *no* device can be discovered anymore.
MAINLOOP_TIMEOUT = 60


def run_mainloop():
    """Build a GLib mainloop that auto-quits after MAINLOOP_TIMEOUT seconds."""
    mainloop = GLib.MainLoop()

    def on_timeout():
        print(f"Timed out after {MAINLOOP_TIMEOUT}s waiting for notifications",
              file=sys.stderr)
        mainloop.quit()
        return False

    GLib.timeout_add_seconds(MAINLOOP_TIMEOUT, on_timeout)
    return mainloop


def get_adapter_path(bus):
    # Never hard-code /org/bluez/hci0: the controller's index can change across
    # reboots/updates (it moved hci0 -> hci1 here once), which silently breaks
    # every hard-coded path. Discover the current adapter from ObjectManager.
    om = bus.get("org.bluez", "/")["org.freedesktop.DBus.ObjectManager"]
    for path, ifaces in om.GetManagedObjects().items():
        if "org.bluez.Adapter1" in ifaces:
            return path
    print("No Bluetooth adapter found", file=sys.stderr)
    sys.exit(1)


def get_device(bus, address):
    adapter_path = get_adapter_path(bus)
    dev_path = adapter_path + "/dev_" + address.replace(":", "_")
    try:
        return bus.get("org.bluez", dev_path)
    except KeyError:
        pass

    adapter = bus.get("org.bluez", adapter_path)
    # Only manage discovery ourselves if nothing is already scanning. When
    # dejvice.sh runs one shared scan for the whole batch, per-device
    # StartDiscovery/StopDiscovery churn races the controller and leaves it
    # stuck "discovering" (StopDiscovery -> InProgress), which wedges every
    # subsequent lookup. So we piggy-back on the existing scan and never touch
    # discovery when it's already active.
    started = False
    if not adapter.Discovering:
        try:
            adapter.StartDiscovery()
            started = True
        except GLib.Error as e:
            print(e, file=sys.stderr)

    try:
        N_TRIES = 12
        N_TRY_LENGTH = 5
        for i in range(N_TRIES):
            time.sleep(N_TRY_LENGTH)
            try:
                return bus.get("org.bluez", dev_path)
            except KeyError:
                pass
            print(f"Waiting for device... {i+1}/{N_TRIES}", file=sys.stderr)
        print("Device not found", file=sys.stderr)
        sys.exit(1)
    finally:
        # Only stop discovery if *we* started it; never stop a scan owned by
        # someone else (e.g. dejvice.sh's shared batch scan). A StopDiscovery
        # that itself errors (BlueZ can raise InProgress) must never propagate.
        if started:
            try:
                adapter.StopDiscovery()
            except GLib.Error as e:
                print(e, file=sys.stderr)


def bt_setup(address):
    bus = pydbus.SystemBus()
    device = get_device(bus, address)

    N_TRIES = 3
    for i in range(N_TRIES):
        try:
            device.Connect()
            break
        except GLib.Error as e:
            print(f"Connecting to device... {i+1}/{N_TRIES}", file=sys.stderr)
            print(e, file=sys.stderr)
            time.sleep(1)
    else:
        print("Connection failed", file=sys.stderr)
        sys.exit(1)
    time.sleep(2)  # XXX: wait for services etc. to be populated

    object_manager = bus.get("org.bluez", "/")["org.freedesktop.DBus.ObjectManager"]

    uuid_write = "00010203-0405-0607-0809-0a0b0c0d2b11"
    uuid_read  = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def get_characteristic(uuid):
        # GATT services can take a moment to resolve after Connect(); retry a
        # few times before giving up instead of crashing with IndexError.
        for attempt in range(5):
            matches = [desc for desc in object_manager.GetManagedObjects().items()
                       if desc[0].startswith(device._path) and desc[1].get("org.bluez.GattCharacteristic1", {}).get("UUID") == uuid]
            if matches:
                return matches[0]
            time.sleep(1)
        print(f"Characteristic {uuid} not resolved", file=sys.stderr)
        sys.exit(1)

    write = bus.get("org.bluez", get_characteristic(uuid_write)[0])
    read = bus.get("org.bluez", get_characteristic(uuid_read)[0])
    return device, read, write


def wait_for_temp(read, write):
    raw = []

    def temp_handler(iface, prop_changed, prop_removed):
        if not 'Value' in prop_changed:
            return

        if prop_changed['Value'][0] == 194:
            raw.extend(prop_changed['Value'])
            mainloop.quit()
            return

    read.onPropertiesChanged = temp_handler
    read.StartNotify()
    mainloop = run_mainloop()
    mainloop.run()

    if not raw:
        print("No data received", file=sys.stderr)
        sys.exit(1)

    temp = int.from_bytes(bytes(raw[3:5]), "little", signed=True) / 10
    humid = raw[5]
    return [temp], [humid]


def is_tp357s(device):
    """The TP357S variant speaks a different history protocol; detect it by
    the advertised device name ("TP357S (XXXX)")."""
    try:
        return "TP357S" in (device.Name or "")
    except Exception:
        return False


def get_temperatures_tp357s(read, write, mode):
    """History download for the TP357S variant.

    The TP357S ignores the TP357's 0xa6/0xa7/0xa8 history opcodes (it only
    echoes a live reading). It instead uses a datetime-sync handshake (0xa5)
    followed by a 0xcccc-framed request, reverse-engineered in
    https://github.com/giovannipizzi/pytp357s (see its PROTOCOL.md).

    The device stores minute-resolution records only (up to ~45 days; the
    count field is 16-bit so at most 65535 records per fetch). To keep the
    downstream contract identical to the TP357 (dejvice.sh feeds "day" at
    1-minute steps, backfill.py expects "year" as consecutive hourly samples
    ending at the current hour), "week"/"year" output hourly averages of the
    minute records; "year" simply returns everything the device has.
    """
    if mode == "day":
        count, hourly = 1440, False
    elif mode == "week":
        count, hourly = 7 * 1440, True
    elif mode == "year":
        # The firmware silently ignores requests for more than 28800 records
        # (empirically bisected: 28800 works, 28801 gets no response), which
        # is exactly 20 days x 1440 -- presumably the history buffer size.
        count, hourly = 28800, True
    else:
        raise RuntimeError(f"Unknown mode: {mode}")

    mainloop = GLib.MainLoop()
    chunks = []
    state = {"last_rx": time.time(), "done": False}

    def history_handler(iface, prop_changed, prop_removed):
        if 'Value' not in prop_changed:
            return
        d = bytes(prop_changed['Value'])
        if d[:2] == b"\xcc\xcc":
            # Start of (a new) history stream; drop any earlier partial one.
            chunks.clear()
            chunks.append(d)
        elif chunks:
            if d[:1] == b"\xc2" and len(d) == 7:
                return  # interleaved periodic live reading, not stream data
            chunks.append(d)
        else:
            # Pre-stream chatter (periodic 0xc2 live readings) must not feed
            # the idle timer, or a never-starting stream would never time out.
            return
        state["last_rx"] = time.time()
        if chunks and chunks[-1][-2:] == b"\x66\x66":
            state["done"] = True
            mainloop.quit()

    read.onPropertiesChanged = history_handler
    read.StartNotify()

    now = datetime.datetime.now()
    # Datetime sync is a required handshake: without it the device never
    # responds to history requests on this connection.
    # DOW as weekday()+1 (Mon=1) per the pytp357s reference implementation;
    # the firmware doesn't seem to validate it (nor the checksum) strictly.
    dt = bytes([0xa5, now.year % 100, now.month, now.day,
                now.hour, now.minute, now.second, now.weekday() + 1])
    write.WriteValue(dt + bytes([sum(dt) & 0xff]), {})
    time.sleep(1)

    body = bytes([0x01, 0x09, 0x00, 0x00, 0x00,
                  now.year % 100, now.month, now.day,
                  now.hour, now.minute, now.second,
                  count & 0xff, (count >> 8) & 0xff])
    cmds = [bytes.fromhex("cccc0201000001046666"),  # session init
            bytes.fromhex("cccc04000000046666"),    # offset (ignored by fw)
            b"\xcc\xcc" + body + bytes([sum(body) & 0xff]) + b"\x66\x66"]
    for cmd in cmds:
        write.WriteValue(cmd, {})
        time.sleep(0.2)

    # A full 65535-record transfer takes ~45s, so the flat MAINLOOP_TIMEOUT
    # would be too tight; instead quit when the stream stalls (no notification
    # for a while), with a hard cap as backstop. The cap must stay below
    # dejvice.sh's external `timeout 150` so we normally get to clean up
    # (Disconnect) ourselves instead of being SIGTERMed.
    IDLE_TIMEOUT = 30
    HARD_TIMEOUT = 120
    t0 = time.time()

    def check_progress():
        if state["done"]:
            return False
        if time.time() - state["last_rx"] > IDLE_TIMEOUT:
            print(f"History stream stalled for {IDLE_TIMEOUT}s", file=sys.stderr)
            mainloop.quit()
            return False
        if time.time() - t0 > HARD_TIMEOUT:
            print(f"History transfer exceeded {HARD_TIMEOUT}s", file=sys.stderr)
            mainloop.quit()
            return False
        return True

    GLib.timeout_add_seconds(2, check_progress)
    mainloop.run()

    if not state["done"] or not chunks:
        print(f"No complete history received ({len(chunks)} chunks)",
              file=sys.stderr)
        sys.exit(1)

    # Stream: cc cc 01 [len x3] 00 [temp16le/10 humid8]... [cksum] 66 66
    # where len = records*3 + 1. Validate framing and the declared length so
    # a lost interior chunk can't silently shift triplet alignment and feed
    # garbage positional history into the RRD.
    buf = b"".join(chunks)
    if buf[:3] != b"\xcc\xcc\x01" or buf[-2:] != b"\x66\x66":
        print(f"Malformed history stream framing ({len(buf)} bytes)",
              file=sys.stderr)
        sys.exit(1)
    buf = buf[2:-2]
    pairs = buf[5:-1]
    declared = int.from_bytes(buf[1:4], "little")
    if len(pairs) % 3 or (declared != len(pairs) + 1
                          and int.from_bytes(buf[1:4], "big") != len(pairs) + 1):
        print(f"History stream length mismatch (declared {declared}, "
              f"got {len(pairs) + 1}) -- dropped chunk?", file=sys.stderr)
        sys.exit(1)
    readings = []  # most recent record first, one per minute
    for i in range(0, len(pairs), 3):
        readings.append((int.from_bytes(pairs[i:i+2], "little", signed=True) / 10,
                         pairs[i+2]))
    print(f"Got {len(readings)} minute records", file=sys.stderr)
    if not readings:
        print("History response contained no readings", file=sys.stderr)
        sys.exit(1)

    if not hourly:
        temps = [t for t, h in reversed(readings)]
        humids = [h for t, h in reversed(readings)]
        return temps, humids

    # Aggregate minute records into consecutive hourly means, oldest first,
    # last sample being the current (partial) hour -- backfill.py alignment.
    fetch_epoch = int(now.timestamp()) // 60 * 60
    byhour = {}
    for i, (t, h) in enumerate(readings):
        hour = (fetch_epoch - i * 60) // 3600 * 3600
        byhour.setdefault(hour, []).append((t, h))
    temps = []
    humids = []
    for hr in range(min(byhour), max(byhour) + 3600, 3600):
        if hr in byhour:
            temps.append(round(sum(t for t, h in byhour[hr]) / len(byhour[hr]), 1))
            humids.append(round(sum(h for t, h in byhour[hr]) / len(byhour[hr])))
        else:
            temps.append(float('nan'))
            humids.append(float('nan'))
    return temps, humids


def get_temperatures(read, write, mode):
    raw = []

    if mode == "day":
        op_code = [b"\xa7", b"\x7a"]
    elif mode == "week":
        op_code = [b"\xa6", b"\x6a"]
    elif mode == "year":
        op_code = [b"\xa8", b"\x8a"]
    else:
        raise RuntimeError(f"Unknown mode: {mode}")

    def temp_handler(iface, prop_changed, prop_removed):
        if not 'Value' in prop_changed:
            return

        if prop_changed['Value'][0] == ord(op_code[0]):
            raw.append(prop_changed['Value'])
        elif raw:
            mainloop.quit()
            return

    read.onPropertiesChanged = temp_handler
    read.StartNotify()

    write.AcquireWrite({})
    write.WriteValue(op_code[0] + b"\x01\x00" + op_code[1], {})

    mainloop = run_mainloop()
    mainloop.run()

    if not raw:
        print("No data received", file=sys.stderr)
        sys.exit(1)

    temps = []
    humids = []
    for t in raw:
        if t[0] != ord(op_code[0]):
            continue
        time = t[1] + t[2]*256
        flag = t[3]
        for i in range(5):
            ofs = 4 + i * 3
            if t[ofs] == 0xff and t[ofs + 1] == 0xff:
                temps.append(float('nan'))
                humids.append(float('nan'))
                continue
            temps.append((t[ofs] + t[ofs + 1] * 256) / 10)
            humids.append(t[ofs + 2])
    return temps, humids


if __name__ == "__main__":
    device, read, write = bt_setup(sys.argv[1])

    try:
        if sys.argv[2] == "now":
            temps, humids = wait_for_temp(read, write)
        elif is_tp357s(device):
            temps, humids = get_temperatures_tp357s(read, write, sys.argv[2])
        else:
            temps, humids = get_temperatures(read, write, sys.argv[2])
    finally:
        # Always release the connection, even on timeout/error, so a bad run
        # never leaves the adapter holding a stale session.
        try:
            device.Disconnect()
        except GLib.Error as e:
            print(e, file=sys.stderr)

    import csv
    writer = csv.writer(sys.stdout)
    writer.writerow(["temp", "humid"])
    for i in range(len(temps)):
        writer.writerow([temps[i], humids[i]])

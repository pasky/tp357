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

# Process start, for the whole-process history-transfer deadline (see
# get_temperatures_tp357s): however long discovery/connect took, we must
# finish (incl. cleanup) before dejvice.sh's external `timeout 150` SIGTERMs
# us without running our finally: Disconnect.
_START = time.time()


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
    except (KeyError, GLib.Error) as e:
        print(f"Cannot read device name for variant detection ({e}); "
              f"assuming plain TP357", file=sys.stderr)
        return False


def tp357s_datetime_cmd(now):
    """Datetime-sync command (0xa5): a required handshake, without which the
    TP357S never responds to history requests on this connection.

    DOW is weekday()+1 (Mon=1) per the pytp357s reference implementation
    (whose PROTOCOL.md says Sun=1, contradicting its own code); the firmware
    doesn't seem to validate it (nor the checksum) strictly."""
    p = bytes([0xa5, now.year % 100, now.month, now.day,
               now.hour, now.minute, now.second, now.weekday() + 1])
    return p + bytes([sum(p) & 0xff])


def tp357s_history_cmds(now, count):
    """The three-command 0xcccc-framed history request sequence; write these
    in order, ~200ms apart."""
    body = bytes([0x01, 0x09, 0x00, 0x00, 0x00,
                  now.year % 100, now.month, now.day,
                  now.hour, now.minute, now.second,
                  count & 0xff, (count >> 8) & 0xff])
    # cmd2 here is the 9-byte form from the pytp357s source (its PROTOCOL.md
    # shows a 10-byte variant); the 9-byte form is what we verified working.
    return [bytes.fromhex("cccc0201000001046666"),  # session init
            bytes.fromhex("cccc04000000046666"),    # offset (ignored by fw)
            b"\xcc\xcc" + body + bytes([sum(body) & 0xff]) + b"\x66\x66"]


def tp357s_decode(buf):
    """Decode a reassembled TP357S history stream into a list of
    (temp, humid) tuples, most recent record first, one per minute.

    Stream: cc cc 01 [len x3 LE] 00 [temp16le/10 humid8]... [cksum] 66 66
    where len = records*3 + 1 and cksum = sum of all bytes between the cc cc
    prefix and the cksum byte, & 0xff (both verified against real captures).
    Framing, declared length and checksum are all validated so that a lost
    or corrupted interior chunk can't silently shift triplet alignment and
    feed garbage positional history into the RRD; raises ValueError."""
    if len(buf) < 10 or buf[:3] != b"\xcc\xcc\x01" or buf[-2:] != b"\x66\x66":
        raise ValueError(f"malformed framing ({len(buf)} bytes)")
    inner = buf[2:-2]
    declared = int.from_bytes(inner[1:4], "little")
    pairs = inner[5:-1]
    if len(pairs) % 3 or declared != len(pairs) + 1:
        raise ValueError(f"length mismatch (declared {declared}, got "
                         f"{len(pairs) + 1}) -- dropped chunk?")
    if sum(inner[:-1]) & 0xff != inner[-1]:
        raise ValueError("checksum mismatch")
    return [(int.from_bytes(pairs[i:i+2], "little", signed=True) / 10,
             pairs[i+2])
            for i in range(0, len(pairs), 3)]


def tp357s_hourly(readings, fetch_epoch):
    """Aggregate most-recent-first minute records into consecutive hourly
    means, oldest first, the last sample being the (partial) hour containing
    fetch_epoch -- the alignment backfill.py expects."""
    base = fetch_epoch // 60 * 60
    byhour = {}
    for i, (t, h) in enumerate(readings):
        hour = (base - i * 60) // 3600 * 3600
        byhour.setdefault(hour, []).append((t, h))
    temps = []
    humids = []
    for hr in range(min(byhour), max(byhour) + 3600, 3600):
        if hr in byhour:
            temps.append(round(sum(t for t, h in byhour[hr]) / len(byhour[hr]), 1))
            humids.append(round(sum(h for t, h in byhour[hr]) / len(byhour[hr])))
        else:  # unreachable with consecutive minute records; defensive
            temps.append(float('nan'))
            humids.append(float('nan'))
    return temps, humids


def get_temperatures_tp357s(read, write, mode, epoch_file=None):
    """History download for the TP357S variant.

    The TP357S ignores the TP357's 0xa6/0xa7/0xa8 history opcodes (it only
    echoes a live reading). It instead uses a datetime-sync handshake (0xa5)
    followed by a 0xcccc-framed request, reverse-engineered in
    https://github.com/giovannipizzi/pytp357s (see its PROTOCOL.md).

    The device stores minute-resolution records only, at most 20 days worth
    (see the "year" cap below). To keep the downstream contract identical to
    the TP357 (dejvice.sh feeds "day" at 1-minute steps, backfill.py expects
    "year" as consecutive hourly samples ending at the current hour),
    "week"/"year" output hourly averages of the minute records; "year"
    returns everything the device has.
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

    write.WriteValue(tp357s_datetime_cmd(datetime.datetime.now()), {})
    time.sleep(1)

    # Capture the request time only now: the device aligns its history to the
    # moment it receives the request, and the handshake sleep above (plus all
    # the discovery/connect delays before) could have crossed a minute or
    # hour boundary since our caller's idea of "now".
    now = datetime.datetime.now()
    if epoch_file:
        with open(epoch_file, "w") as f:
            print(int(now.timestamp()), file=f)
    for cmd in tp357s_history_cmds(now, count):
        write.WriteValue(cmd, {})
        time.sleep(0.2)

    # A full 28800-record transfer takes ~20s, so the flat MAINLOOP_TIMEOUT
    # could be too tight; instead quit when the stream stalls (no
    # notification for a while), with a whole-process deadline as backstop,
    # kept below dejvice.sh's external `timeout 150` (however long
    # discovery/connect already took) so we normally get to clean up
    # (Disconnect) ourselves instead of being SIGTERMed.
    IDLE_TIMEOUT = 30
    HARD_DEADLINE = _START + 135

    def check_progress():
        if state["done"]:
            return False
        if time.time() - state["last_rx"] > IDLE_TIMEOUT:
            print(f"History stream stalled for {IDLE_TIMEOUT}s", file=sys.stderr)
            mainloop.quit()
            return False
        if time.time() > HARD_DEADLINE:
            print("History transfer exceeded the process deadline",
                  file=sys.stderr)
            mainloop.quit()
            return False
        return True

    GLib.timeout_add_seconds(2, check_progress)
    mainloop.run()

    if not state["done"] or not chunks:
        print(f"No complete history received ({len(chunks)} chunks)",
              file=sys.stderr)
        sys.exit(1)

    try:
        readings = tp357s_decode(b"".join(chunks))
    except ValueError as e:
        print(f"Bad history stream: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Got {len(readings)} minute records", file=sys.stderr)
    if not readings:
        print("History response contained no readings", file=sys.stderr)
        sys.exit(1)

    if not hourly:
        return ([t for t, h in reversed(readings)],
                [h for t, h in reversed(readings)])
    return tp357s_hourly(readings, int(now.timestamp()))


def get_temperatures(read, write, mode, epoch_file=None):
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
    if epoch_file:
        with open(epoch_file, "w") as f:
            print(int(time.time()), file=f)
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
            temps.append(int.from_bytes(bytes(t[ofs:ofs + 2]), "little",
                                        signed=True) / 10)
            humids.append(t[ofs + 2])
    return temps, humids


if __name__ == "__main__":
    args = sys.argv[1:]
    epoch_file = None
    if "--epoch-file" in args:
        # File to record the epoch of the moment the history request was
        # issued -- the timestamp the device aligns its history to, which
        # backfill.py needs (the caller's own clock readings are unreliable:
        # discovery/connect delays before, transfer time after).
        i = args.index("--epoch-file")
        epoch_file = args[i + 1]
        del args[i:i + 2]
    address, mode = args[0], args[1]

    device, read, write = bt_setup(address)

    try:
        if mode == "now":
            temps, humids = wait_for_temp(read, write)
        elif is_tp357s(device):
            temps, humids = get_temperatures_tp357s(read, write, mode,
                                                    epoch_file)
        else:
            temps, humids = get_temperatures(read, write, mode, epoch_file)
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

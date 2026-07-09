#!/usr/bin/env python
# -*- coding: utf-8 -*-

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

    temp = (raw[3] + raw[4] * 256) / 10
    humid = raw[5]
    return [temp], [humid]


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

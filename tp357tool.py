#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time

from gi.repository import GLib
import pydbus


def get_device(bus, address):
    try:
        return bus.get("org.bluez", "/org/bluez/hci0/dev_" + address.replace(":", "_"))
    except KeyError:
        adapter = bus.get("org.bluez", "/org/bluez/hci0")
        adapter.StartDiscovery()
        N_TRIES = 12
        N_TRY_LENGTH = 5
        for i in range(N_TRIES):
            time.sleep(N_TRY_LENGTH)
            try:
                device = bus.get("org.bluez", "/org/bluez/hci0/dev_" + address.replace(":", "_"))
                break
            except KeyError:
                pass
            print(f"Waiting for device... {i+1}/{N_TRIES}", file=sys.stderr)
        else:
            adapter.StopDiscovery()
            print("Device not found", file=sys.stderr)
            sys.exit(1)
        adapter.StopDiscovery()
        return device


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
        return [desc for desc in object_manager.GetManagedObjects().items()
                if desc[0].startswith(device._path) and desc[1].get("org.bluez.GattCharacteristic1", {}).get("UUID") == uuid][0]

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
    mainloop = GLib.MainLoop()
    mainloop.run()

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

    mainloop = GLib.MainLoop()
    mainloop.run()

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

    if sys.argv[2] == "now":
        temps, humids = wait_for_temp(read, write)
    else:
        temps, humids = get_temperatures(read, write, sys.argv[2])

    device.Disconnect()

    import csv
    writer = csv.writer(sys.stdout)
    writer.writerow(["temp", "humid"])
    for i in range(len(temps)):
        writer.writerow([temps[i], humids[i]])

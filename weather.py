#!/usr/bin/env python3
"""Feed outside/living-room RRDs from the Wunderground-protocol weather station.

A local weather station (ID=brm) POSTs readings to
/weatherstation/updateweatherstation.php on the `pasky` vhost every few
seconds. Apache logs each request, so we parse the access log instead of
running a server. Relevant query params:

    tempf, humidity            -> outside temperature / humidity
    indoortempf, indoorhumidity -> living room (inside) temperature / humidity

Temperatures are Fahrenheit and converted to Celsius. Readings are aggregated
per minute (the RRD step) and only timestamps newer than each RRD's last
update are fed, so re-runs over an overlapping log are idempotent.

Usage:
    weather.py [LOGFILE ...]

Defaults to /var/log/apache2/pasky.access_log. Pass rotated logs (incl. .gz)
oldest-first to backfill, e.g.:
    weather.py $(ls -tr /var/log/apache2/pasky.access_log*)
"""

import gzip
import os
import re
import subprocess
import sys
from datetime import datetime
from urllib.parse import parse_qs

DEFAULT_LOG = "/var/log/apache2/pasky.access_log"
HERE = os.path.dirname(os.path.realpath(__file__))

# rrd name -> (temp param, humid param)
RRDS = {
    "outside": ("tempf", "humidity"),
    "livingroom": ("indoortempf", "indoorhumidity"),
}

LINE_RE = re.compile(
    r'\[(?P<ts>\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\] '
    r'"GET /weatherstation/updateweatherstation\.php\?(?P<qs>\S+) '
)


def f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def open_log(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return open(path, "rt", errors="replace")


def rrd_last(rrd_path):
    if not os.path.exists(rrd_path):
        return 0
    out = subprocess.run(
        ["rrdtool", "last", rrd_path], capture_output=True, text=True, check=True
    ).stdout.strip()
    return int(out)


def collect(logs):
    # name -> { minute_epoch: [ (temp_c, humid), ... ] }
    buckets = {name: {} for name in RRDS}
    for path in logs:
        with open_log(path) as f:
            for line in f:
                m = LINE_RE.search(line)
                if not m:
                    continue
                ts = int(datetime.strptime(
                    m.group("ts"), "%d/%b/%Y:%H:%M:%S %z").timestamp())
                minute = ts - (ts % 60)
                q = parse_qs(m.group("qs"))
                for name, (tp, hp) in RRDS.items():
                    if tp not in q or hp not in q:
                        continue
                    try:
                        tc = f_to_c(float(q[tp][0]))
                        hv = float(q[hp][0])
                    except ValueError:
                        continue
                    buckets[name].setdefault(minute, []).append((tc, hv))
    return buckets


def feed(name, minutes):
    rrd_path = os.path.join(HERE, name + ".rrd")
    last = rrd_last(rrd_path)
    updates = []
    for minute in sorted(minutes):
        if minute <= last:
            continue
        samples = minutes[minute]
        temp = sum(s[0] for s in samples) / len(samples)
        humid = sum(s[1] for s in samples) / len(samples)
        updates.append("%d:%.2f:%.1f" % (minute, temp, humid))
    if not updates:
        print("%s: nothing new (last=%d)" % (name, last))
        return
    # Chunk to stay under ARG_MAX; timestamps stay increasing across chunks.
    for i in range(0, len(updates), 2000):
        subprocess.run(
            ["rrdtool", "update", rrd_path, "--"] + updates[i:i + 2000],
            check=True)
    print("%s: fed %d minute samples" % (name, len(updates)))


def main():
    logs = sys.argv[1:] or [DEFAULT_LOG]
    buckets = collect(logs)
    for name in RRDS:
        feed(name, buckets[name])


if __name__ == "__main__":
    main()

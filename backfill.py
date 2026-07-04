#!/usr/bin/env python3
"""Backfill RRD gaps from a TP357 `year` history CSV.

The thermometers keep hourly history for ~365 days. The daily cron only feeds
the RRD with `day` (last 24h) data, so any cron downtime leaves a gap that the
normal `rrdtool update` cannot fill (it refuses timestamps older than the last
update). This tool dumps the RRD, fills the NaN rows that fall inside the
device's hourly history, and restores it -- existing real data is never
touched.

Alignment (verified against overlap): the LAST sample in the `year` CSV is the
current wall-clock hour, each previous sample is one hour earlier.

Usage:
    backfill.py NAME.rrd year.csv [--fetch-epoch EPOCH] [--apply]

Without --apply it only reports what would be filled (dry run).
"""

import csv
import math
import os
import re
import subprocess
import sys
import time

ROW_RE = re.compile(
    r'^(?P<head>\s*<!-- .* / (?P<ts>\d+) -->) <row><v>(?P<temp>\S+)</v><v>(?P<humid>\S+)</v></row>\s*$'
)


def load_year_csv(path, fetch_epoch):
    """Return {hour_epoch: (temp, humid)} from an oldest-first year CSV."""
    rows = []
    with open(path, newline="") as f:
        r = csv.reader(f)
        header = next(r)
        assert header[:2] == ["temp", "humid"], f"unexpected header {header}"
        for line in r:
            if not line:
                continue
            t, h = line[0].strip(), line[1].strip()
            rows.append((t, h))
    # Last sample == current hour floor at fetch time; each prior is -3600s.
    hour_floor = (fetch_epoch // 3600) * 3600
    n = len(rows)
    data = {}
    for k, (t, h) in enumerate(rows):  # k = 0..n-1, oldest first
        ts = hour_floor - (n - 1 - k) * 3600
        try:
            tv = float(t)
            hv = float(h)
        except ValueError:
            continue
        if math.isnan(tv):
            continue
        data[ts] = (tv, hv)
    return data, n


def fmt(v):
    return "%.10e" % v


def backfill(rrd_path, data):
    dump = subprocess.run(
        ["rrdtool", "dump", rrd_path], check=True, capture_output=True, text=True
    ).stdout
    out_lines = []
    filled = 0
    for line in dump.splitlines():
        m = ROW_RE.match(line)
        if m and m.group("temp") == "NaN":
            ts = int(m.group("ts"))
            hour = (ts // 3600) * 3600
            sample = data.get(hour)
            if sample is not None:
                tv, hv = sample
                line = (
                    f'{m.group("head")} <row><v>{fmt(tv)}</v><v>{fmt(hv)}</v></row>'
                )
                filled += 1
        out_lines.append(line)
    return "\n".join(out_lines) + "\n", filled


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    args = [a for a in args if a != "--apply"]
    fetch_epoch = int(time.time())
    if "--fetch-epoch" in args:
        i = args.index("--fetch-epoch")
        fetch_epoch = int(args[i + 1])
        del args[i : i + 2]
    rrd_path, csv_path = args[0], args[1]

    data, n = load_year_csv(csv_path, fetch_epoch)
    span_lo = min(data) if data else 0
    span_hi = max(data) if data else 0
    print(
        f"[{rrd_path}] year csv: {n} rows, {len(data)} valid hourly samples "
        f"covering {time.strftime('%Y-%m-%d %H:%M', time.localtime(span_lo))} "
        f".. {time.strftime('%Y-%m-%d %H:%M', time.localtime(span_hi))}"
    )

    xml, filled = backfill(rrd_path, data)
    print(f"[{rrd_path}] would fill {filled} NaN rows (1m + 1h RRAs combined)")

    if not apply:
        print("  dry run -- pass --apply to write")
        return

    tmp_xml = rrd_path + ".backfill.xml"
    new_rrd = rrd_path + ".backfill.new"
    with open(tmp_xml, "w") as f:
        f.write(xml)
    if os.path.exists(new_rrd):
        os.remove(new_rrd)
    subprocess.run(["rrdtool", "restore", tmp_xml, new_rrd], check=True)
    # sanity: last update preserved
    old_last = subprocess.run(["rrdtool", "last", rrd_path], capture_output=True, text=True).stdout.strip()
    new_last = subprocess.run(["rrdtool", "last", new_rrd], capture_output=True, text=True).stdout.strip()
    assert old_last == new_last, f"lastupdate changed {old_last} -> {new_last}"
    bak = rrd_path + ".bak-" + time.strftime("%Y%m%d")
    if not os.path.exists(bak):
        os.rename(rrd_path, bak)
    else:
        os.remove(rrd_path)
    os.rename(new_rrd, rrd_path)
    os.remove(tmp_xml)
    print(f"  applied. backup at {bak}")


if __name__ == "__main__":
    main()

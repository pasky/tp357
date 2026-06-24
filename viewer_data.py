#!/usr/bin/env python3
"""Export all RRD series to JSON for the static overlay viewer.

Produces two resolutions next to this script:
  viewer-fine.json   - last ~8 days at 5 min step (for short windows)
  viewer-coarse.json - last ~400 days at 1 h step (for long windows)

Each file is rrdtool xport's native JSON: meta.{start,step,legend} plus a
`data` array of rows aligned to the time grid (ts = start + i*step). Legend
entries are "<location>.<metric>", e.g. "outside.temp", "kitchen.humid".
"""

import json
import os
import subprocess

HERE = os.path.dirname(os.path.realpath(__file__))

# Display order; outside/living room first, then the BLE room sensors.
LOCATIONS = [
    "outside", "livingroom", "pasky", "kitchen", "bedroom",
    "storage", "chido", "bathroomc", "bathroomp",
]

RESOLUTIONS = {
    "fine": ("end-8d", 300),
    "coarse": ("end-400d", 3600),
}


def export(start, step):
    cmd = ["rrdtool", "xport", "--json", "--maxrows", "12000",
           "--start", start, "--end", "now", "--step", str(step)]
    for loc in LOCATIONS:
        rrd = os.path.join(HERE, loc + ".rrd")
        if not os.path.exists(rrd):
            continue
        cmd += [
            "DEF:%s_t=%s:temp:AVERAGE" % (loc, rrd),
            "DEF:%s_h=%s:humid:AVERAGE" % (loc, rrd),
            "XPORT:%s_t:%s.temp" % (loc, loc),
            "XPORT:%s_h:%s.humid" % (loc, loc),
        ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    # rrdtool emits bare NaN tokens which are invalid JSON; normalise to null.
    out = out.replace("NaN", "null").replace("nan", "null")
    return json.loads(out)


def main():
    for name, (start, step) in RESOLUTIONS.items():
        data = export(start, step)
        path = os.path.join(HERE, "viewer", "viewer-%s.json" % name)
        with open(path, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        rows = len(data.get("data", []))
        print("%s: %d rows, step %ds -> %s" % (name, rows, step, path))


if __name__ == "__main__":
    main()

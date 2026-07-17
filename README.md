ThermoPro TP357 Temperature Sensor Client
=========================================

This tiny Python client for the BLE bluetooth sensor TP357 offers a CLI to
retrieve the current temperature and humidity (the sensor periodically sends a
notification) as well as fetch the history that the sensor stores (up to a
year, reportedly).

TP357 is a pretty nifty temperature and humidity sensor ideal for e.g.
monitoring individual rooms in a flat. It is tiny and cheap, has both display
and bluetooth, has battery life (single AAA) of around 6 months, and seems
pretty accurate.

Usage: `tp357tool.py ADDRESS MODE`

**ADDRESS** - hardware address of the device; use bluetoothctl + "scan on" + "devices" to find it

**MODE** - "now" (current temperature), "day" (minute-by-minute over the last 24 hours) or "week" (hour-by-hour over the last 7 days) or "year" (hour-by-hour over the last 365 days)

Example: `./tp357tool.py B8:59:CE:32:9C:D1 now`

Outputs a CSV file with temperature and humidity time series, oldest first.

TP357S Variant
--------------

The newer TP357S variant (auto-detected from its advertised name) speaks a
completely different history protocol -- the TP357 opcodes above only produce
a live-reading echo on it. The implementation follows the protocol
reverse-engineered by https://github.com/giovannipizzi/pytp357s (see its
PROTOCOL.md): a 0xa5 datetime-sync handshake followed by a 0xcccc-framed
request. Differences to be aware of:

* The device stores minute-resolution records only, up to ~45 days (16-bit
  record count). "year" returns everything it has.
* "week"/"year" output hourly averages of the minute records, so the output
  format (and backfill.py alignment) matches the TP357.
* A battery pull appears to clear the stored history.
* "now" works unchanged (same 0xc2 notification format).

History and Plots with RRD
--------------------------

We can use e.g. `rrdtool` to store high precision and long-term historical
temperature data and plot them.

Example:

	rrdtool create sensor.rrd --start -2d --step 1m DS:temp:GAUGE:1h:-50:100 DS:humid:GAUGE:12h:0:100 RRA:AVERAGE:0.5:1m:1y RRA:AVERAGE:0.5:1h:10y
	./tp357tool.py B8:59:CE:32:9C:D1 day | tail -n +2 | tac | sed 's/\r$//' |
		{ a=0; while IFS=, read temp humid; do if [ "$a" = 0 ]; then A=N; else A=$a; fi; echo "$A:$temp:$humid"; a=$((a-60)); done; } | tac |
		xargs rrdtool update sensor.rrd -s --
	rrdtool graph sensor-1d.png --end now --start end-1d --width 1440 --height 280 -l 0 -u 40 --right-axis 2.5:0 --right-axis-format %.0lf%% DEF:temp=sensor.rrd:temp:AVERAGE DEF:humidr=sensor.rrd:humid:AVERAGE CDEF:humid=humidr,2.5,/ LINE1:temp#ff0000 LINE1:humid#0000ff

Backfilling Gaps
----------------

The daily cron (`dejvice.sh`) only feeds the RRD with `day` (last 24h) data, so
any downtime longer than a day leaves a gap that plain `rrdtool update` cannot
fill (it refuses timestamps older than the last update). `backfill.py` dumps
the RRD and fills the NaN rows that fall inside the device's hourly `year`
history (existing real data is never touched):

	./tp357tool.py ADDRESS year > year.csv
	./backfill.py NAME.rrd year.csv --apply   # omit --apply for a dry run

`dejvice.sh` does this automatically: for each device it checks `rrdtool last`
*before* the `day` fetch, and if the RRD is stale by more than a day it pulls
the `year` history and backfills the gap.

Weather Station (outside / living room)
--------------------------------------

Besides the BLE sensors, a Wunderground-protocol weather station (`ID=brm`)
POSTs readings every few seconds to
`/weatherstation/updateweatherstation.php` on the `pasky` apache vhost.
`weather.py` scrapes those readings out of the apache access log (no server
needed), converts the Fahrenheit temperatures to Celsius, aggregates per
minute, and feeds two RRDs: `outside.rrd` (`tempf`/`humidity`) and
`livingroom.rrd` (`indoortempf`/`indoorhumidity`). It only feeds timestamps
newer than each RRD's last update, so re-runs are idempotent.

	./weather.py                 # incremental, reads /var/log/apache2/pasky.access_log
	./weather.py $(ls -tr /var/log/apache2/pasky.access_log*)   # backfill from rotated logs

Reading the apache logs requires membership in the `adm` group
(`sudo usermod -aG adm pasky`). `dejvice.sh` runs `weather.py` and renders the
`outside-*.png` / `livingroom-*.png` graphs alongside the room graphs.

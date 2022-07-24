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

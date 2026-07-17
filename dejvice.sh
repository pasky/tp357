#!/bin/bash
# Collect TP357 sensor data into RRDs, render graphs, publish to web.
# Runs from its own directory; uses the local .venv (system-site-packages
# venv providing PyGObject/gi + pydbus) so it works under cron.
cd "$(dirname "$(readlink -f "$0")")" || exit 1

# Prevent overlapping runs: cron fires every 2h, but a slow/hung BLE fetch can
# outlast that. Without this guard, runs stack up and their leaked adapter
# sessions eventually wedge the controller ("Device not found" for everything).
exec 9>"$PWD/.dejvice.lock"
if ! flock -n 9; then
	echo "another dejvice.sh is still running -- skipping this run"
	exit 0
fi

PY="$PWD/.venv/bin/python"
# Hard wall-clock cap for any single BLE fetch, as a backstop to the in-tool
# timeout, so a wedged D-Bus call can never hang a run indefinitely.
BTFETCH=(timeout 150 "$PY")
PUBDIR="$HOME/WWW/dejvice/tp357"

devices="pasky=B8:59:CE:32:9C:D1 kitchen=B8:59:CE:33:0A:A4 storage=B8:59:CE:33:34:57 bedroom=FD:A2:F4:E1:AA:B0 chido=B8:59:CE:32:82:0B bathroomp=10:76:36:19:21:9A bathroomc=B8:59:CE:34:33:8A"

# NB: each tp357tool.py handles its own BLE discovery per device (discover ->
# stop -> connect). Do NOT hold a shared scan open across the loop: a single
# radio can't reliably scan and connect at once, so an active scan makes every
# Connect() fail with le-connection-abort-by-local.
for d in $devices; do
	echo $d
	IFS== read name addr <<<$d
	echo $name $addr
	#"$PY" tp357tool.py $addr now

	# Note how stale the RRD is *before* we feed it fresh data. The `day`
	# fetch below only covers the last 24h, so if the last update is older
	# than that (cron downtime, laptop asleep, ...) it leaves a gap that
	# plain `rrdtool update` can never fill (it refuses old timestamps).
	now=$(date +%s)
	last=$(rrdtool last $name.rrd 2>/dev/null || echo 0)

	"${BTFETCH[@]}" tp357tool.py $addr day | tail -n +2 | tac | sed 's/\r$//' |
		{ a=0; while IFS=, read temp humid; do if [ "$a" = 0 ]; then A=N; else A=$a; fi; echo "$A:$temp:$humid"; a=$((a-60)); done; } | tac |
		xargs rrdtool update $name.rrd -s --

	# Stale by more than a day: backfill the pre-`day` gap from the device's
	# hourly `year` history (up to ~365 days on the TP357, only ~20 days on
	# the TP357S). backfill.py only touches NaN rows, so the fresh
	# minute-resolution data we just wrote is preserved.
	if [ $((now - last)) -gt 86400 ]; then
		echo "  last update $(((now - last) / 3600))h ago -- backfilling from year history"
		yearcsv=$(mktemp)
		epochf=$(mktemp)
		# tp357tool.py records in $epochf the moment it issued the history
		# request -- that's what the device aligns its history to. Neither a
		# pre-invocation clock reading (discovery/connect can take a minute)
		# nor backfill.py's own (the transfer can take minutes) reliably
		# falls into the same hour, which would shift the whole backfilled
		# series by an hour.
		if "${BTFETCH[@]}" tp357tool.py $addr year --epoch-file "$epochf" >"$yearcsv"; then
			fepoch=$(cat "$epochf" 2>/dev/null)
			"$PY" backfill.py $name.rrd "$yearcsv" --fetch-epoch "${fepoch:-$(date +%s)}" --apply
		fi
		rm -f "$yearcsv" "$epochf"
	fi
	rrdtool graph $name-1d.png --end now --start end-1d --width 720 --height 280 -l 0 -u 100 --left-axis-format %.0lf%% --right-axis 0.18:14 --right-axis-format %.0lf DEF:temp=$name.rrd:temp:AVERAGE DEF:humid=$name.rrd:humid:AVERAGE CDEF:temps=temp,14,-,0.18,/ LINE1:temps#ff0000 LINE1:humid#0000ff
	rrdtool graph $name-1w.png --end now --start end-1w --width 720 --height 280 -l 0 -u 100 --left-axis-format %.0lf%% --right-axis 0.18:14 --right-axis-format %.0lf DEF:temp=$name.rrd:temp:AVERAGE DEF:humid=$name.rrd:humid:AVERAGE CDEF:temps=temp,14,-,0.18,/ LINE1:temps#ff0000 LINE1:humid#0000ff

done

# Outside + living room come from the Wunderground-protocol weather station
# (ID=brm) that POSTs to /weatherstation/updateweatherstation.php on the pasky
# vhost; we scrape its readings out of the apache access log.
"$PY" weather.py
# outside: wider temp axis (-20..40 C); livingroom: indoor axis (14..32 C)
for name in outside livingroom; do
	if [ "$name" = outside ]; then raxis="0.6:-20"; cdef="temp,-20,-,0.6,/"; else raxis="0.18:14"; cdef="temp,14,-,0.18,/"; fi
	for span in 1d 1w; do
		rrdtool graph $name-$span.png --end now --start end-$span --width 720 --height 280 -l 0 -u 100 --left-axis-format %.0lf%% --right-axis $raxis --right-axis-format %.0lf DEF:temp=$name.rrd:temp:AVERAGE DEF:humid=$name.rrd:humid:AVERAGE CDEF:temps=$cdef LINE1:temps#ff0000 LINE1:humid#0000ff
	done
done

# Refresh JSON data for the interactive overlay viewer (viewer/).
"$PY" viewer_data.py

# Publish graphs + data to the web directory served at https://pasky.or.cz/dejvice/tp357/
mkdir -p "$PUBDIR"
rsync -a --no-perms --no-owner --no-group *.png *.rrd "$PUBDIR"/
rsync -a --no-perms --no-owner --no-group viewer "$PUBDIR"/

#!/bin/bash
# Status of a sweep in flight, drawn from the two things that actually know:
# the run's log (which bands were asked, and what they answered) and the DB
# (which rows have landed since it started).
#
# There is no per-listing progress to read. `rent-assistant scrape` shows a click
# progressbar, and click prints only the label when stdout is not a tty — so
# a redirected run leaves nothing behind between "Collected N list cards" and
# "Done." Rows are upserted one at a time inside the detail loop, which makes
# the DB the live counter: new rows per minute is the real rate.
#
# Usage: scripts/scrape-status.sh [logfile]   (default: data/sweep/fresh.log)

set -uo pipefail

DATA=~/AI/rent-assistant/data
DB="$DATA/rentals.db"
LOG=${1:-$DATA/sweep/fresh.log}

[ -r "$LOG" ] || { echo "no log at $LOG"; exit 1; }

# Only the last run in the file: these logs append across runs.
run=$(awk '/=== .* start:/ { buf = "" } { buf = buf $0 "\n" } END { printf "%s", buf }' "$LOG")

started_line=$(echo "$run" | grep -m1 '=== .* start:')
started=$(echo "$started_line" | sed -n 's/^\[\([^]]*\)\].*/\1/p')
rows_at_start=$(echo "$started_line" | sed -n 's/.*start: \([0-9]*\) rows.*/\1/p')

if pgrep -f "[f]b-rent scrape" > /dev/null; then
  state="running"
elif echo "$run" | grep -q '=== done:'; then
  state="finished"
else
  state="STOPPED — neither running nor finished"
fi

rows=$(sqlite3 "$DB" "select count(*) from listings;")
missing=$(sqlite3 "$DB" "select count(*) from listings where description is null or description='';")

echo "run:     $state, started $started"
echo "bands:"
# A band that answered exactly --max hit our own ceiling, not Facebook's:
# there is more behind it. Flag it — that is the top-up signal.
maxitems=$(echo "$started_line" | sed -n 's/.*max=\([0-9]*\).*/\1/p')
echo "$run" | awk -v cap="${maxitems:-0}" '
  /^\[[0-9]+\/[0-9]+\] [0-9]+-[0-9]+ THB:/ { band = $2; next }
  band && /cards\.$/ {
    n = $1
    printf "  %-14s %4d cards%s\n", band, n, (cap && n >= cap ? "   <- hit our --max, more behind it" : "")
    band = ""
  }'
collected=$(echo "$run" | sed -n 's/^Collected \([0-9]*\) list cards.*/\1/p' | tail -1)
[ -n "$collected" ] && echo "         $collected collected in total" || echo "         (still collecting cards)"

echo "rows:    $rows total"
if [ -n "$rows_at_start" ]; then
  echo "         +$((rows - rows_at_start)) this run"
  # Minutes elapsed, from the log's own timestamp — the run may predate this shell.
  s=$(date -j -f '%Y-%m-%d %H:%M:%S' "$started" +%s 2>/dev/null)
  if [ -n "$s" ]; then
    mins=$(( ( $(date +%s) - s ) / 60 ))
    [ "$mins" -gt 0 ] && echo "         $mins min elapsed, $(( (rows - rows_at_start) / (mins > 0 ? mins : 1) )) rows/min"
  fi
fi
echo "         $missing without description"

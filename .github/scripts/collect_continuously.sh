#!/usr/bin/env bash
#
# Collect once, or keep collecting for a few hours.
#
# GitHub's scheduler has not run this workflow once: six consecutive slots
# passed with the workflow active and its syntax valid, across two different
# cron expressions. Until that changes, a single manual start has to be able to
# do more than take one reading, because free-flow baselines need observations
# spread across hours and a lone snapshot can only be compared against a posted
# speed limit.
#
# So: RUN_FOR_HOURS=5 turns one click into a full afternoon of collection at
# INTERVAL_MINUTES apart. RUN_FOR_HOURS=0, the default and what the schedule
# uses, takes exactly one snapshot and stops.
#
# This cannot restart itself. A job may re-dispatch a workflow through the API,
# but GitHub deliberately ignores workflow_dispatch events authenticated with
# the built-in GITHUB_TOKEN, precisely to stop workflows looping forever. Doing
# it anyway needs a personal access token, which only the repository owner can
# create.

set -euo pipefail

HOURS="${RUN_FOR_HOURS:-0}"
INTERVAL_MINUTES="${INTERVAL_MINUTES:-20}"
COLLECT="$(dirname "$0")/collect_traffic.sh"

if [ -z "$HOURS" ] || [ "$HOURS" = "0" ]; then
  exec bash "$COLLECT"
fi

# The runner kills the job at six hours, so never plan past five and a half.
SECONDS_TOTAL=$(python3 -c "print(int(min(float('$HOURS'), 5.5) * 3600))")
INTERVAL=$((INTERVAL_MINUTES * 60))
DEADLINE=$(( $(date +%s) + SECONDS_TOTAL ))

PLANNED=$(python3 -c "
seconds = $SECONDS_TOTAL
hours, minutes = divmod(round(seconds / 60), 60)
print(f'{hours}h {minutes}m' if hours else f'{minutes}m')
")
echo "Collecting every ${INTERVAL_MINUTES} minutes for ${PLANNED}, until $(date -u -d "@$DEADLINE" '+%H:%M UTC' 2>/dev/null || echo 'the deadline')."

attempts=0
failures=0
while :; do
  attempts=$((attempts + 1))
  echo "::group::Collection ${attempts}"
  # One bad collection must not end a five-hour run. The published feed
  # intermittently answers a valid query with nothing, and the collector exits
  # non-zero rather than publishing an empty report over a good one; that is a
  # reason to try again in twenty minutes, not to stop.
  if bash "$COLLECT"; then
    echo "Collection ${attempts} published."
  else
    failures=$((failures + 1))
    echo "Collection ${attempts} failed; continuing."
  fi
  echo "::endgroup::"

  now=$(date +%s)
  if [ $((now + INTERVAL)) -ge "$DEADLINE" ]; then
    break
  fi
  sleep "$INTERVAL"
done

echo "Finished: ${attempts} collections, ${failures} of them failed."

# Silence for hours is not success. If nothing at all got through, the run
# should be red.
if [ "$failures" -eq "$attempts" ]; then
  echo "Every collection failed." >&2
  exit 1
fi

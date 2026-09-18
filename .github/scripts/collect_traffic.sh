#!/usr/bin/env bash
#
# Collect one NYC DOT traffic snapshot and publish it to the data branch.
#
# Run by .github/workflows/nyc-traffic.yml. Results go to a branch of their own
# rather than to the default branch: at a snapshot every fifteen minutes this
# commits about a hundred times a day, which would bury the project's real
# history.
#
# Environment:
#   DATA_BRANCH   branch to publish to (default: traffic-data)
#   HISTORY_DAYS  days of link history to retain (default: 3)
#   KEEP_RUNS     timestamped per-run CSVs to retain (default: 96, one day)
#   IMAGES        "true" to also capture a still from every camera
#   PYTHON_BIN    interpreter to run (default: python)
#
# Phone alerts are enabled automatically when credentials for a transport are
# present (NTFY_TOPIC, or Pushover, or Telegram). NOTIFY_LEVEL and
# NOTIFY_CORRIDORS tune what is worth interrupting someone for.

set -euo pipefail

BRANCH="${DATA_BRANCH:-traffic-data}"
HISTORY_DAYS="${HISTORY_DAYS:-3}"
KEEP_RUNS="${KEEP_RUNS:-96}"
PYTHON_BIN="${PYTHON_BIN:-python}"
WORKTREE="${RUNNER_TEMP:-/tmp}/nyc-traffic-data"
OUTPUT="$WORKTREE/traffic_data"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Work in a linked worktree so the checked-out code is never disturbed: the
# scraper has to keep running from the default branch while its output is
# committed to another.
rm -rf "$WORKTREE"
git worktree prune

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "Data branch $BRANCH exists; continuing its history."
  git fetch --no-tags origin "$BRANCH"
  git worktree add -B "$BRANCH" "$WORKTREE" "origin/$BRANCH"
else
  echo "Data branch $BRANCH does not exist yet; creating it."
  git worktree add --detach "$WORKTREE"
  git -C "$WORKTREE" checkout --orphan "$BRANCH"
  # The orphan branch inherits the code in its index. Clear it so the data
  # branch holds only data, and no .gitignore that would exclude the CSVs.
  git -C "$WORKTREE" rm -rf --quiet . >/dev/null 2>&1 || true
fi

mkdir -p "$OUTPUT"

ARGUMENTS=(--output "$OUTPUT" --history-days "$HISTORY_DAYS")

# Alerting turns itself on when there is somewhere to send to. Without this the
# collector would need a separate switch that could drift out of step with
# whether the credentials are actually set.
if [ -n "${NTFY_TOPIC:-}" ] || [ -n "${PUSHOVER_USER_KEY:-}" ] || [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "Alert transport configured; phone alerts are on."
  ARGUMENTS+=(--notify)
  if [ -n "${NOTIFY_LEVEL:-}" ]; then
    ARGUMENTS+=(--notify-level "$NOTIFY_LEVEL")
  fi
  if [ -n "${NOTIFY_CORRIDORS:-}" ]; then
    ARGUMENTS+=(--notify-corridors "$NOTIFY_CORRIDORS")
  fi
else
  echo "No alert transport configured; collecting quietly."
fi

ARGUMENTS+=(snapshot)
if [ "${IMAGES:-false}" = "true" ]; then
  ARGUMENTS+=(--images)
fi

echo "Collecting snapshot..."
"$PYTHON_BIN" -m etl.nycdot "${ARGUMENTS[@]}"

# Per-run CSVs accumulate one set per snapshot. Keep a rolling window; the
# history file and the latest report carry everything that is read afterwards.
prune_old() {
  local pattern="$1"
  # shellcheck disable=SC2012 - filenames here are timestamps, so ls sorts right
  ls -1 "$OUTPUT"/$pattern 2>/dev/null | sort -r | tail -n "+$((KEEP_RUNS + 1))" \
    | while read -r stale; do rm -f "$stale"; done
}
prune_old "links_*.csv"
prune_old "cameras_*.csv"
prune_old "assessed_links_*.csv"
prune_old "corridors_*.csv"
prune_old "report_*.md"

# Camera stills are far larger than the CSVs, so keep only the most recent sets.
if [ -d "$OUTPUT/stills" ]; then
  ls -1d "$OUTPUT"/stills/*/ 2>/dev/null | sort -r | tail -n +5 \
    | while read -r stale; do rm -rf "$stale"; done
fi

cat > "$WORKTREE/README.md" <<'MD'
# NYC East Side traffic data

Collected automatically by `.github/workflows/nyc-traffic.yml` on the default
branch. This branch holds only data: nothing here is edited by hand, and it is
force-updated on a schedule.

* `traffic_data/latest_report.md` — the most recent snapshot, in plain English.
* `traffic_data/history/YYYY-MM-DD.csv` — one append-only file per day of link
  readings, which is what the free-flow baselines are measured from. Older days
  are deleted rather than rewritten.
* `traffic_data/assessed_links_*.csv` — per-segment congestion verdicts.
* `traffic_data/corridors_*.csv` — corridor rollups.

See `docs/NYCDOT_TRAFFIC.md` on the default branch for what the numbers mean and
what they cannot say.
MD

git -C "$WORKTREE" add -A
if git -C "$WORKTREE" diff --cached --quiet; then
  echo "Nothing changed; not committing."
  exit 0
fi

git -C "$WORKTREE" commit -q -m "Traffic snapshot $(date -u +'%Y-%m-%d %H:%M UTC') [skip ci]"
git -C "$WORKTREE" push -q origin "$BRANCH"
echo "Published to $BRANCH."

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

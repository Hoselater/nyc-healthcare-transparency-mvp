# NYC DOT traffic: live link speeds and the camera network

A standalone scraper for the two public NYC Department of Transportation feeds,
focused on the East Side of Manhattan. It shares this repository but not its
pipeline: nothing here touches Postgres, SPARCS, or the healthcare app, and it
needs no credentials.

## Status

The first live run happened on 18 September 2026, from GitHub Actions. The
camera feed worked first time: 979 cameras, 183 of them on the East Side. The
speed feed did not, and what it taught us is written into the code.

This code still cannot be run from every environment. The sandbox it was
developed in blocks every NYC and New York State data host at its egress proxy,
so `webcams.nyctmc.org` and `data.cityofnewyork.us` both refuse the connection
before TLS. GitHub's runners have ordinary internet access, which is why
collection lives there.

### What the first live run found

The speed dataset is **an archive, not a snapshot**. It holds one row per link
per observation and keeps growing, and Socrata returns rows in no defined order
unless asked. The first run therefore pulled a million rows that happened to be
an arbitrary slice of the past few months, found a median age of two weeks, and
correctly concluded it had nothing current to say. The fix is to ask for the
newest rows explicitly and keep only the most recent reading of each link.

The feed also answers inconsistently. The same windowed query returned 749
rows, then zero fourteen minutes later with an identical watermark, then
normally again. An empty answer is not evidence that the roads are empty, so a
window that comes back empty while the watermark says data exists is retried as
a differently shaped query before being believed. And a snapshot that measures
nothing never overwrites one that did: the last good report stays put and the
run fails loudly instead of reporting a quiet success over lost data.

Staleness moved with it. Measuring each reading against the wall clock means
that when the publisher falls behind, every link is branded stale and the
snapshot reports nothing at all. A sensor is now stale when it lags *the rest of
the feed*, and how far behind the feed itself is runs at the top of the report
as a separate fact, because that says nothing about traffic.

## The two feeds

### Live link speeds

`https://data.cityofnewyork.us/resource/i4gi-tjb9.json` mirrors DOT's real-time
sensor feed. One record per directional road segment, refreshed roughly once a
minute, with a current speed in miles per hour, a travel time in seconds, and
the polyline of the roadway the segment covers.

What it is not: a complete picture. Coverage is highways and major arterials,
so most avenue blocks have no sensor at all. A street missing from the output is
unmeasured, not empty.

Three quirks the code handles, each of which will otherwise produce a confident
wrong answer:

* **Zeroes are dropped sensors**, not stopped traffic, so they are excluded from
  every average and counted separately in the report.
* **`data_as_of` is local New York time with no offset marker.** Read as UTC,
  every reading looks four or five hours stale. Readings that come out in the
  future are flagged, because that is what a change of convention would look
  like.
* **The publisher sometimes falls behind.** A sensor counts as stale when it
  lags the rest of the feed, not the wall clock, so a feed running an hour late
  still yields a usable picture of that hour. How far behind the feed is appears
  at the top of the report as its own statement.
* **There is no published free-flow speed**, so congestion has no denominator
  unless you supply one. See below.

### Traffic cameras

`https://webcams.nyctmc.org/api/cameras` returns the public camera inventory:
an id, a name, a point location, an online flag, and a still-image URL per
camera. The stills refresh every few seconds and are not archived, so a still
is only ever evidence about the moment it was pulled.

Cameras measure nothing. Their job here is corroboration: when a segment reports
6 mph, the nearest camera tells you whether that is traffic, a closed lane, or a
sensor talking nonsense. Every congested segment in the report carries the
nearest camera within half a mile.

## Defining "the East Side"

Membership is decided from coordinates, not from the feeds' own borough labels,
which are too coarse to be useful on a river crossing. Two selectors, combined,
and every row records which one matched:

1. **A polygon** whose western edge follows Broadway below 8th Street and Fifth
   Avenue above it, and whose eastern edge follows the East River shoreline
   pushed about 150 m offshore so the FDR Drive and the Manhattan ends of the
   crossings fall inside. It runs from the Battery to 125th Street. A camera
   inside it, or a segment with any vertex inside it, is in scope.
2. **Corridor names**, for records whose geometry is missing or malformed: the
   FDR, the Harlem River Drive, the Queensboro Bridge, the Queens-Midtown
   Tunnel, the RFK, the Williamsburg, Manhattan and Brooklyn bridges, the East
   Side avenues and the numbered cross streets.

The polygon is drawn deliberately a little wide and is not an administrative
boundary. It lives in `etl/nycdot/geo.py` as a list of annotated coordinates;
edit it there if you want a different area.

## How congestion is measured

The feed gives a speed and nothing to compare it against, so the reference is
supplied here, in one of two ways, and every output row says which one it used.

**Observed baseline, preferred.** With enough history for a segment, its
85th-percentile observed speed becomes its free-flow speed. This is the honest
reference: it is what that road actually does when it is moving, measured by the
same sensor, so sensor bias cancels out.

The collection window matters as much as the number of readings. A segment
observed thirty times, all between five and six on a weekday evening, has an
85th-percentile speed that *is* its congested speed, and comparing it against
itself would report a jam as free flow. So a baseline is only trusted once the
readings span at least six hours, and one that still lands below half the posted
limit is labelled as probably an all-peak window rather than quietly believed.

**Posted-limit assumption, fallback.** With no usable history, the reference is
assumed from the road class: 50 mph for the FDR and Harlem River Drive, 35 for
the East River crossings, 25 for arterials, which is New York City's default
limit. Percentages built this way are indicative, not measured, and the report
says so and counts how many segments relied on it.

Bands are ratios of current speed to free flow: free flow at 80% or above,
moderate from 60%, heavy from 40%, severe below that. The ratio caps at 100%,
because nothing flows more freely than free flow; a segment running above its
reference means the assumption is too low for that road.

Corridor speeds are averaged over distance rather than over segments: total
distance divided by the time taken to cover it, which is what a driver
experiences. A tenth-of-a-mile ramp cannot count as much as three miles of the
FDR, and averaging the segment speeds arithmetically would overstate the result
besides.

That arithmetic needs lengths, and **the published geometry cannot be trusted
for length**. The same stretch of FDR has been published at 4.74 miles
northbound and 0.54 miles southbound. The feed offers an independent measure,
since speed multiplied by travel time is the distance the sensor itself
measured, and a length the two do not agree on within a factor of two is not
used: no delay is shown for that segment, it is left out of the distance
arithmetic, and the report says how many segments that affected. A corridor
with no trustworthy length falls back to a plain average and labels itself as
such rather than inventing a distance to weight by.

## Running it

```bash
python -m etl.nycdot snapshot            # both feeds, plus a Markdown report
python -m etl.nycdot snapshot --images   # also save a still from every camera
python -m etl.nycdot cameras             # the camera inventory alone
python -m etl.nycdot speeds              # the live speeds alone
python -m etl.nycdot watch --interval 300 --duration 86400
python -m etl.nycdot report              # rebuild a report from saved history
python -m etl.nycdot --all-nyc snapshot  # citywide, no East Side filter
```

Output lands in `exports/nycdot/` (git-ignored): a timestamped CSV of segments,
one of cameras, one of assessed segments with congestion verdicts and the
nearest camera, a corridor summary, a Markdown report, and `history/<date>.csv`,
which every run appends to.

**Run `watch` first.** A single snapshot can only use posted-limit assumptions.
One pass across a full weekday and the quiet hours after it gives every segment
a measured baseline, and the percentages stop being guesses. A day at five
minute intervals is about 288 pulls and a few tens of megabytes of CSV.

An app token is optional and raises Socrata's rate limit. Register free at
`data.cityofnewyork.us` and set `NYC_OPEN_DATA_APP_TOKEN`; it is sent as a
header, not a query parameter, so it stays out of logs.

## Running it without a computer

The collector runs on GitHub's runners, which have ordinary internet access, and
publishes to a branch called `traffic-data` so the project's own history stays
readable.

**It is manual only. There is no schedule, and nothing starts on its own.** To
collect, open the Actions tab, pick "NYC East Side traffic", and choose "Run
workflow". The default takes one snapshot and stops. Setting `run_for_hours`
keeps it collecting every twenty minutes for that many hours, up to five and a
half, and any run can be cancelled from its own page at any time.

Read the result on a phone by bookmarking this, which always shows the most
recent snapshot:

```
https://github.com/<owner>/<repo>/blob/traffic-data/traffic_data/latest_report.md
```

GitHub renders the Markdown, so it is readable on a phone without downloading
anything.

### Why there is no schedule

There was one, and it never fired. Six consecutive slots passed without a single
scheduled run, across two different cron expressions, with the workflow
`state: active` and its syntax valid on the default branch throughout:

```
03:40  04:00  04:20     cron */20
04:47  05:07  05:27     cron 7,27,47, moved off the hour deliberately
```

It has since been removed outright rather than left in as clutter that might
start something unattended. Collection is something you ask for.

**What this costs you:** a single snapshot can only be compared against a posted
speed limit. The measured free-flow baselines the report prefers need readings
spanning six hours, so they only appear if you choose to collect for that long.
Until then the percentages are labelled as assumptions, which is what they are.

### Where this can run

| Where | Works? |
| --- | --- |
| GitHub Actions | Yes. One click collects for up to five hours and can push alerts to your phone. The cron schedule has never fired. |
| Home computer | Yes, Python 3.9 or newer and `pip install requests` |
| Phone, reading results | Yes, the report renders in a browser, and alerts arrive as notifications |
| Phone, running the scraper | Possible via a terminal app, but a watch loop needs a machine that stays awake |
| This sandbox | No, the data hosts are blocked by its network policy |

## Getting alerts on your phone

The collector can push a notification when a corridor turns severe, and again
when it clears. Alerting stays off until a transport is configured, so nothing
here happens by accident.

### What it sends, and what it does not

A snapshot every twenty minutes is seventy-two a day. Sending one notification
per snapshot is how an app gets silenced, so alerts fire on *change* only:

* when a corridor first crosses into the alert level, and again if it gets
  worse than the level you were already told about;
* when it has stayed below that level for half an hour, which is the all-clear.
  A single good reading is not enough, because traffic dips for one reading all
  the time and an all-clear sent into a jam that is still there is worse than
  silence;
* never twice for the same ongoing jam, and never more than once every ninety
  minutes for a corridor flapping either side of the line.

If several corridors turn bad in the same snapshot, which is normal at rush
hour, they arrive as one summary rather than four separate buzzes. A simulated
day with two rush periods produces four notifications; a quiet day produces
none.

The default level is **severe**, meaning under 40% of free flow. Set the
repository variable `NOTIFY_LEVEL` to `heavy` for a lower bar, and
`NOTIFY_CORRIDORS` to a comma-separated list to limit which roads can wake you.

### Setting it up with ntfy (no account needed)

1. Install **ntfy** on your phone, from the App Store or Google Play.
2. Pick a topic name nobody could guess. ntfy topics are unauthenticated by
   default, so the name *is* the password. Something like
   `nyc-traffic-939c728fbf59`.
3. In the app, tap **+** and subscribe to that topic.
4. In this repository on GitHub, go to **Settings → Secrets and variables →
   Actions → New repository secret**. Name it `NTFY_TOPIC` and paste the topic
   name as the value.
5. Test it from the **Actions** tab: pick "NYC East Side traffic", then **Run
   workflow**. If any corridor is severe, the alert arrives within a minute.

That is the whole setup. There is nothing to install on a computer and no
account to create.

### Or Pushover, or Telegram

Set these as repository secrets instead; whichever is present wins, in this
order.

| Transport | Secrets | Notes |
| --- | --- | --- |
| ntfy | `NTFY_TOPIC` | Free, no account. `NTFY_SERVER` and `NTFY_TOKEN` for a self-hosted or protected instance. |
| Pushover | `PUSHOVER_USER_KEY`, `PUSHOVER_APP_TOKEN` | One-off purchase per platform. |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Free; create the bot with BotFather. |

A failed notification never fails a collection run, and an alert that could not
be delivered is not recorded as sent, so the next run tries again rather than
staying silent forever.

### Running alerts from your own machine

```bash
export NTFY_TOPIC=nyc-traffic-939c728fbf59
python -m etl.nycdot --notify --notify-level heavy snapshot
```

Alert state is kept in `alert_state.json` next to the other outputs. Deleting it
resets what you have been told, so the next run alerts on everything currently
bad.

## Retention

History is partitioned by day: one append-only file per date under
`traffic_data/history/`. Retention deletes whole day files rather than rewriting
live ones, so a run that dies midway cannot leave a truncated history behind,
and git stores an append rather than a fresh copy of a growing file every twenty
minutes. `--history-days` sets the window; the scheduled collector keeps three
days, which is well past what a baseline needs.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Ninety-nine offline tests, no internet access needed. The unit tests cover the cases
that actually broke during development: naive timestamps read in the wrong
timezone, malformed polylines, cameras at 0/0, baselines derived from too narrow
a window, and appends to a CSV whose header has since gained a column. The
command line tests run a whole snapshot against fixtures served from a loopback
HTTP server, checking that the region filter, the outputs, the camera join and
the placeholder-image rejection all still work together. The alert tests are
mostly about staying quiet: an ongoing jam, a flapping corridor, a single good
reading and an hour of the feed returning nothing must all produce silence, and
a simulated day of collection must produce a handful of notifications rather
than seventy. A feed outage announcing that the jam is over would be the worst
failure of the lot, so it has a test of its own.

## Terms of use

Both feeds are public and unauthenticated. The camera stills are published by
the NYC Traffic Management Centre for public viewing; the cameras are
deliberately low resolution and are not intended to identify people or vehicles.
Poll at intervals of a minute or more. The speed feed updates about once a
minute, so anything faster re-reads the same numbers at the city's expense.

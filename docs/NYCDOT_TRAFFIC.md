# NYC DOT traffic: live link speeds and the camera network

A standalone scraper for the two public NYC Department of Transportation feeds,
focused on the East Side of Manhattan. It shares this repository but not its
pipeline: nothing here touches Postgres, SPARCS, or the healthcare app, and it
needs no credentials.

## Status: no live data has been collected yet

The code was written and tested in a sandbox whose egress policy blocks every
NYC and New York State data host, so **no real reading has ever passed through
it**. Every number you have seen from it so far came from fixtures. The blocked
hosts were:

| Host | Feed | Result |
| --- | --- | --- |
| `webcams.nyctmc.org` | camera inventory and stills | `403` at the proxy, CONNECT refused |
| `data.cityofnewyork.us` | live link speeds (`i4gi-tjb9`) | `403` at the proxy, CONNECT refused |
| `data.ny.gov`, `511ny.org` | state alternatives | `403` at the proxy, CONNECT refused |

Run it from a machine with ordinary internet access and it will collect real
data. Until someone does, treat the field names below as what the feeds
published as of the last time they were documented, and read the first run's
warnings carefully: the normalisers are written to tolerate renamed and
malformed fields rather than to assume the documentation is current.

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
moderate from 60%, heavy from 40%, severe below that. Corridor figures are
weighted by segment length, so a tenth-of-a-mile ramp cannot count as much as
three miles of the FDR.

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
nearest camera, a corridor summary, a Markdown report, and `speed_history.csv`,
which every run appends to.

**Run `watch` first.** A single snapshot can only use posted-limit assumptions.
One pass across a full weekday and the quiet hours after it gives every segment
a measured baseline, and the percentages stop being guesses. A day at five
minute intervals is about 288 pulls and a few tens of megabytes of CSV.

An app token is optional and raises Socrata's rate limit. Register free at
`data.cityofnewyork.us` and set `NYC_OPEN_DATA_APP_TOKEN`; it is sent as a
header, not a query parameter, so it stays out of logs.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Forty offline tests, no internet access needed. The unit tests cover the cases
that actually broke during development: naive timestamps read in the wrong
timezone, malformed polylines, cameras at 0/0, baselines derived from too narrow
a window, and appends to a CSV whose header has since gained a column. The
command line tests run a whole snapshot against fixtures served from a loopback
HTTP server, checking that the region filter, the outputs, the camera join and
the placeholder-image rejection all still work together.

## Terms of use

Both feeds are public and unauthenticated. The camera stills are published by
the NYC Traffic Management Centre for public viewing; the cameras are
deliberately low resolution and are not intended to identify people or vehicles.
Poll at intervals of a minute or more. The speed feed updates about once a
minute, so anything faster re-reads the same numbers at the city's expense.

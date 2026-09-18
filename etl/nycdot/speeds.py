"""NYC DOT real-time link speeds.

DOT runs a network of roadway sensors and publishes the state of each
directional segment ("link") to NYC Open Data as dataset ``i4gi-tjb9``
("DOT Traffic Speeds NBE"), which updates about once a minute.

**The dataset is an archive, not a snapshot.** It holds one row per link per
observation and keeps accumulating, so it runs to millions of rows covering
months. Socrata returns rows in no defined order unless asked, so a plain
request for the first N rows hands back an arbitrary slice of history: the
first live run of this collector pulled a million rows whose median age was two
weeks, and concluded, correctly but uselessly, that it had nothing current.

Reading the current state therefore means asking for the newest rows
explicitly (``$order=data_as_of DESC``) and keeping only the most recent
observation of each link, which is what :func:`latest_per_link` does.

Caveats that matter when reading the numbers:

* Coverage is highways and major arterials. There is no link on most avenue
  blocks, so an absent street is not a quiet street.
* ``speed`` is the sensor's current average, in miles per hour. Zero almost
  always means a dropped sensor rather than stopped traffic, so zeroes are
  flagged and excluded from averages instead of being read as gridlock.
* ``data_as_of`` is local New York time with no offset marker. A link whose
  timestamp is hours old is a stale sensor, and is flagged as such.
* There is no published free-flow speed per link, so any congestion measure has
  to supply its own reference. See :mod:`etl.nycdot.analyze`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import requests

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    NEW_YORK = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata
    NEW_YORK = None  # type: ignore[assignment]

from etl.nycdot import geo
from etl.nycdot.fetch import FeedUnavailable, first_present, get_json

log = logging.getLogger(__name__)

SOCRATA_DOMAIN = "data.cityofnewyork.us"
SPEEDS_DATASET_ID = "i4gi-tjb9"
SPEEDS_URL = f"https://{SOCRATA_DOMAIN}/resource/{SPEEDS_DATASET_ID}.json"

# The feed DOT serves directly, behind the Open Data mirror. Kept as a fallback
# for when the mirror lags or is down; it carries the same columns under
# camelCase names, which the shared normaliser already handles.
RAW_FEED_URL = "https://data.cityofnewyork.us/api/views/i4gi-tjb9/rows.json"

# Safety cap on rows fetched. The watermark query normally keeps the real number
# in the hundreds; this only bounds the fallback path.
MAX_RECORDS = 50_000
# How far back from the feed's newest reading to collect. Wide enough to catch
# links that report less often than the busiest ones, narrow enough that each
# link contributes only a handful of rows.
CURRENT_WINDOW_MINUTES = 30
STALE_AFTER_MINUTES = 15

FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "link_id": ("link_id", "linkId", "id"),
    "speed_mph": ("speed",),
    "travel_time_seconds": ("travel_time", "travelTime"),
    "status": ("status",),
    "data_as_of": ("data_as_of", "dataAsOf", "last_updated"),
    "link_points": ("link_points", "linkPoints"),
    "link_name": ("link_name", "linkName", "name"),
    "borough": ("borough", "boroughs"),
    "owner": ("owner",),
    "transcom_id": ("transcom_id", "transcomId"),
}


@dataclass
class SpeedLink:
    """One directional road segment at one instant."""

    link_id: str
    link_name: str | None
    speed_mph: float | None
    travel_time_seconds: float | None
    status: str | None
    data_as_of_local: str | None
    age_minutes: float | None
    lag_minutes: float | None
    is_stale: bool
    borough: str | None
    owner: str | None
    length_miles: float | None
    implied_speed_mph: float | None
    latitude: float | None
    longitude: float | None
    point_count: int
    corridor: str | None
    road_class: str
    in_region: bool
    match_reason: str | None
    observed_at_utc: str

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def parse_feed_timestamp(value: Any) -> datetime | None:
    """Parse ``data_as_of`` into an aware UTC datetime.

    The feed publishes local New York time with no offset ("2026-09-17T18:03:31.000").
    Reading that as UTC would make every reading look four or five hours stale,
    so a naive timestamp is localised to New York before conversion. A value
    that already carries an offset is trusted as-is.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None

    if parsed.tzinfo is None:
        if NEW_YORK is not None:
            parsed = parsed.replace(tzinfo=NEW_YORK)
        else:  # pragma: no cover - no tzdata; assume Eastern standard offset
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=-5)))
    return parsed.astimezone(timezone.utc)


def polyline_length_miles(points: Sequence[tuple[float, float]]) -> float | None:
    """Sum of the great-circle distances between consecutive vertices."""
    if len(points) < 2:
        return None
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        total += geo.haversine_miles(lat1, lon1, lat2, lon2)
    return round(total, 4)


def normalise_link(
    record: dict[str, Any],
    *,
    region: geo.Region | None = geo.EAST_SIDE,
    now_utc: datetime | None = None,
) -> SpeedLink | None:
    """One raw record to a SpeedLink, or None when it has no link id."""
    now_utc = now_utc or datetime.now(timezone.utc)
    values = {
        target: first_present(record, candidates)
        for target, candidates in FIELD_CANDIDATES.items()
    }

    link_id = values["link_id"]
    if link_id in (None, ""):
        return None
    link_id = str(link_id).strip()

    name = values["link_name"]
    link_name = str(name).strip() if name not in (None, "") else None

    points = geo.parse_link_points(values["link_points"])
    latitude, longitude = geo.polyline_midpoint(points)

    as_of = parse_feed_timestamp(values["data_as_of"])
    age_minutes = None
    if as_of is not None:
        age_minutes = round((now_utc - as_of).total_seconds() / 60.0, 1)

    speed = _as_float(values["speed_mph"])
    travel_time = _as_float(values["travel_time_seconds"])
    length_miles = polyline_length_miles(points)

    # An independent read on the same segment: the sensor reports both a speed
    # and a travel time, and the polyline gives a length. When the two disagree
    # badly, one of them is wrong, and the report says so rather than averaging
    # a bad number into a corridor.
    implied_speed = None
    if length_miles and travel_time and travel_time > 0:
        implied_speed = round(length_miles / (travel_time / 3600.0), 1)

    corridor = geo.corridor_for(link_name)
    in_region = False
    match_reason = None
    if region is not None:
        if points and region.contains_any(points):
            in_region = True
            match_reason = "polyline crosses polygon"
        elif corridor and not points:
            in_region = True
            match_reason = f"name matched {corridor} (no geometry)"

    return SpeedLink(
        link_id=link_id,
        link_name=link_name,
        speed_mph=speed,
        travel_time_seconds=travel_time,
        status=str(values["status"]).strip() if values["status"] not in (None, "") else None,
        data_as_of_local=str(values["data_as_of"]) if values["data_as_of"] not in (None, "") else None,
        age_minutes=age_minutes,
        # Both are filled in by mark_staleness once the whole batch is known:
        # a link is stale relative to the rest of the feed, not to the clock.
        lag_minutes=None,
        is_stale=False,
        borough=str(values["borough"]).strip() if values["borough"] not in (None, "") else None,
        owner=str(values["owner"]).strip() if values["owner"] not in (None, "") else None,
        length_miles=length_miles,
        implied_speed_mph=implied_speed,
        latitude=latitude,
        longitude=longitude,
        point_count=len(points),
        corridor=corridor,
        road_class=geo.road_class(corridor),
        in_region=in_region,
        match_reason=match_reason,
        observed_at_utc=now_utc.isoformat(timespec="seconds"),
    )


def _unwrap(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "error" in payload or "message" in payload:
            raise FeedUnavailable(
                f"The speed feed returned an error: {payload.get('message') or payload}"
            )
    raise FeedUnavailable(
        "The speed feed returned a shape this parser does not recognise "
        f"({type(payload).__name__}); expected a JSON array of links."
    )


def latest_per_link(links: Sequence[SpeedLink]) -> list[SpeedLink]:
    """One row per link: the most recent observation of each.

    The archive holds every past reading, so without this a single link
    contributes dozens of rows and the corridor averages become an average over
    history rather than a picture of now.
    """
    newest: dict[str, SpeedLink] = {}
    for link in links:
        current = newest.get(link.link_id)
        if current is None:
            newest[link.link_id] = link
            continue
        # A smaller age is a more recent reading. A row with no readable
        # timestamp loses to one that has a timestamp.
        if link.age_minutes is None:
            continue
        if current.age_minutes is None or link.age_minutes < current.age_minutes:
            newest[link.link_id] = link
    return list(newest.values())


def feed_age_minutes(links: Sequence[SpeedLink]) -> float | None:
    """How old the freshest reading in the feed is, in minutes.

    This is the currency of the data itself, and is reported separately from
    any individual sensor's lag. A feed that is hours behind is a fact about the
    source, not about traffic.
    """
    ages = [link.age_minutes for link in links if link.age_minutes is not None]
    return min(ages) if ages else None


def mark_staleness(
    links: Sequence[SpeedLink], *, stale_after_minutes: float = STALE_AFTER_MINUTES
) -> float | None:
    """Flag sensors lagging the rest of the feed. Returns the feed's own age.

    Staleness is measured against the freshest reading in the batch rather than
    against the wall clock. A sensor that stopped reporting an hour ago is a
    dropped sensor whichever way you measure it, but if the whole feed is
    running an hour behind, every link would otherwise be branded stale and the
    snapshot would report nothing at all. That is a fact about the publisher,
    and it belongs in the report rather than in the filter.
    """
    newest = feed_age_minutes(links)
    if newest is None:
        return None

    for link in links:
        if link.age_minutes is None:
            link.lag_minutes = None
            link.is_stale = False
            continue
        link.lag_minutes = round(link.age_minutes - newest, 1)
        link.is_stale = link.lag_minutes > stale_after_minutes
    return newest


def feed_watermark(
    session: requests.Session,
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[datetime | None, str | None]:
    """The newest ``data_as_of`` in the dataset, as (UTC datetime, raw string).

    One aggregate query, one row back. Knowing where the data actually ends is
    what makes it possible to ask for "the current state" of an archive that may
    be minutes or hours behind: scanning a fixed number of newest rows instead
    wastes most of them on repeat readings of whichever links report most often.
    The first live run scanned fifty thousand rows to find a hundred and
    twenty-five links.
    """
    payload = get_json(
        session, url, params={"$select": "max(data_as_of) as newest"}, headers=headers
    )
    rows = _unwrap(payload)
    if not rows:
        return None, None
    raw = rows[0].get("newest") or rows[0].get("max_data_as_of")
    return parse_feed_timestamp(raw), (str(raw) if raw else None)


def _local_literal(moment: datetime) -> str:
    """Format a UTC instant as the naive New York string the feed compares against."""
    local = moment.astimezone(NEW_YORK) if NEW_YORK else moment
    return local.strftime("%Y-%m-%dT%H:%M:%S.000")


def fetch_speeds(
    session: requests.Session,
    *,
    url: str = SPEEDS_URL,
    app_token: str | None = None,
    region: geo.Region | None = geo.EAST_SIDE,
    max_records: int = MAX_RECORDS,
    window_minutes: float = CURRENT_WINDOW_MINUTES,
) -> list[SpeedLink]:
    """Pull the current state of every link, normalise it, and tag the region.

    Asks for the newest rows explicitly. Socrata's default order is undefined,
    and on a multi-million-row archive that means an arbitrary slice of the past
    rather than the present.

    Anonymous requests are throttled hard; an app token (free, from
    ``data.cityofnewyork.us``) raises the limit and is passed as a header rather
    than a query parameter so it stays out of logs.
    """
    headers = {"X-App-Token": app_token} if app_token else None
    now_utc = datetime.now(timezone.utc)

    def scan() -> list[dict[str, Any]]:
        """The blunt query: newest rows first, no filter. Slower, but sturdy."""
        log.info("Scanning the %d newest readings", max_records)
        return _unwrap(
            get_json(
                session,
                url,
                params={"$limit": max_records, "$order": "data_as_of DESC"},
                headers=headers,
            )
        )

    # Find where the data ends, then ask for the window just behind it.
    watermark = None
    raw = None
    try:
        watermark, raw = feed_watermark(session, url, headers)
        if watermark:
            log.info("Feed's newest reading is stamped %s (local)", raw)
    except FeedUnavailable as exc:
        log.warning("Could not read the feed watermark, scanning instead: %s", exc)

    if watermark is None:
        records = scan()
    else:
        cutoff = _local_literal(watermark - timedelta(minutes=window_minutes))
        log.info("Fetching readings from the %d minutes before it", window_minutes)
        records = _unwrap(
            get_json(
                session,
                url,
                params={
                    "$where": f"data_as_of > '{cutoff}'",
                    "$order": "data_as_of DESC",
                    "$limit": max_records,
                },
                headers=headers,
            )
        )
        if not records:
            # The watermark says data exists up to a given moment, and the
            # window behind that moment came back empty. Those two answers
            # contradict each other, and the API has been observed serving both
            # within a quarter of an hour for the same query. Do not believe the
            # empty one: ask a differently shaped question before giving up.
            log.warning(
                "The windowed query returned nothing although the feed reports "
                "readings up to %s. Falling back to a scan.",
                raw,
            )
            records = scan()

    log.info("Feed returned %d rows", len(records))

    observations: list[SpeedLink] = []
    skipped = 0
    for record in records:
        link = normalise_link(record, region=region, now_utc=now_utc)
        if link is None:
            skipped += 1
            continue
        observations.append(link)

    if skipped:
        log.warning("Skipped %d speed records with no link id", skipped)

    links = latest_per_link(observations)
    newest_age = mark_staleness(links)

    log.info(
        "%d observations covering %d distinct links", len(observations), len(links)
    )
    if newest_age is not None:
        log.info("Freshest reading in the feed is %.1f minutes old", newest_age)
        if newest_age > 60:
            log.warning(
                "The feed's newest reading is %.0f minutes old. The published "
                "dataset is lagging; the snapshot describes that moment, not now.",
                newest_age,
            )

    if len(observations) >= max_records:
        # The window was truncated by the cap, so links whose newest reading sits
        # further back are missing entirely.
        log.warning(
            "Hit the %d row cap; some links may be missing. Narrow window_minutes "
            "or raise max_records.",
            max_records,
        )

    if region is not None:
        log.info(
            "%d links touch %s",
            sum(1 for link in links if link.in_region),
            region.name,
        )
    stale = sum(1 for link in links if link.is_stale)
    if stale:
        log.warning(
            "%d links lag the rest of the feed by more than %d minutes",
            stale,
            STALE_AFTER_MINUTES,
        )

    future = sum(1 for link in links if link.age_minutes is not None and link.age_minutes < -5)
    if future:
        log.warning(
            "%d links are timestamped in the future. The feed publishes local New "
            "York time with no offset and this parser assumes that; if the feed has "
            "switched to UTC, parse_feed_timestamp needs updating and every age in "
            "this run is wrong by the offset.",
            future,
        )
    return links


def in_region(links: Iterable[SpeedLink]) -> list[SpeedLink]:
    return [link for link in links if link.in_region]


def usable(links: Iterable[SpeedLink]) -> list[SpeedLink]:
    """Links fit to average: fresh, and reporting a speed above zero.

    A zero reading is a dropped sensor far more often than it is stopped
    traffic, and a stale reading describes a different hour of the day. Both
    are counted in the report and excluded from the statistics.
    """
    return [
        link
        for link in links
        if link.speed_mph is not None and link.speed_mph > 0 and not link.is_stale
    ]

"""NYC DOT real-time link speeds.

DOT runs a network of roadway sensors and publishes the current state of each
directional segment ("link") as a live feed, mirrored on NYC Open Data as
dataset ``i4gi-tjb9`` ("DOT Traffic Speeds NBE"). It refreshes about once a
minute and is a snapshot, not a history: nothing accumulates unless you keep
the snapshots yourself, which is what :func:`append_history` is for.

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

PAGE_SIZE = 50_000
MAX_PAGES = 20
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
        is_stale=bool(age_minutes is not None and age_minutes > STALE_AFTER_MINUTES),
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


def fetch_speeds(
    session: requests.Session,
    *,
    url: str = SPEEDS_URL,
    app_token: str | None = None,
    region: geo.Region | None = geo.EAST_SIDE,
    page_size: int = PAGE_SIZE,
) -> list[SpeedLink]:
    """Pull the current state of every link, normalise it, and tag the region.

    Anonymous Socrata requests are throttled hard; an app token (free, from
    ``data.cityofnewyork.us``) raises the limit and is passed as a header rather
    than a query parameter so it stays out of logs.
    """
    headers = {"X-App-Token": app_token} if app_token else None
    now_utc = datetime.now(timezone.utc)

    records: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        params = {"$limit": page_size, "$offset": page * page_size}
        log.info("Fetching speed links, page %d", page + 1)
        payload = get_json(session, url, params=params, headers=headers)
        batch = _unwrap(payload)
        records.extend(batch)
        if len(batch) < page_size:
            break
    else:
        log.warning("Stopped after %d pages; the feed may have more rows", MAX_PAGES)

    links: list[SpeedLink] = []
    skipped = 0
    for record in records:
        link = normalise_link(record, region=region, now_utc=now_utc)
        if link is None:
            skipped += 1
            continue
        links.append(link)

    if skipped:
        log.warning("Skipped %d speed records with no link id", skipped)

    log.info("Speed feed: %d links", len(links))
    if region is not None:
        log.info(
            "%d links touch %s",
            sum(1 for link in links if link.in_region),
            region.name,
        )
    stale = sum(1 for link in links if link.is_stale)
    if stale:
        log.warning("%d links carry a timestamp older than %d minutes", stale, STALE_AFTER_MINUTES)

    # A reading cannot be from the future. If many are, the feed has changed its
    # timezone convention and parse_feed_timestamp is now localising UTC
    # timestamps to New York, putting every age out by four or five hours.
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

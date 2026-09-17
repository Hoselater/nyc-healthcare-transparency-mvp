"""Geography for the East Side of Manhattan: polygon test, corridors, distance.

Neither feed carries a "neighbourhood" field worth trusting -- the speed feed's
``borough`` column is coarse (a link crossing the Queensboro Bridge is filed
under one borough or the other, not both) and the camera feed's ``area`` is a
free-text label. So membership is decided here, from coordinates, with a
name-based fallback for records whose geometry is missing or malformed.

Two selectors, combined with OR, and every match records *why* it matched:

1. **Polygon** -- a point (camera) or any vertex of a polyline (speed link)
   falling inside :data:`EAST_SIDE_POLYGON`.
2. **Corridor name** -- the record's name matching a known East Side corridor
   (FDR Drive, the Queensboro Bridge, the Queens-Midtown Tunnel, ...). This
   catches links whose ``link_points`` string is empty, which happens.

The polygon is an approximation, deliberately drawn a little wide, and is not a
legal or administrative boundary. Its western edge follows Broadway below 8th
Street and Fifth Avenue above it; its eastern edge follows the East River
shoreline pushed roughly 150 m offshore so that the FDR Drive -- which is built
out over the water in places -- and the Manhattan ends of the East River
crossings fall inside rather than just outside.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# (latitude, longitude) pairs, traced anticlockwise from the Battery: north up
# the west edge (Broadway, then Fifth Avenue), east along 125th Street, then
# south down the East River shoreline.
EAST_SIDE_POLYGON: tuple[tuple[float, float], ...] = (
    (40.7033, -74.0140),  # The Battery
    (40.7145, -74.0072),  # Broadway & Chambers St
    (40.7190, -74.0020),  # Broadway & Canal St
    (40.7265, -73.9955),  # Broadway & Houston St
    (40.7325, -73.9968),  # Fifth Ave & 8th St (Washington Square North)
    (40.7484, -73.9857),  # Fifth Ave & 34th St
    (40.7644, -73.9732),  # Fifth Ave & 59th St (Grand Army Plaza)
    (40.7852, -73.9573),  # Fifth Ave & 96th St
    (40.8055, -73.9386),  # Fifth Ave & 125th St
    (40.8015, -73.9285),  # Harlem River at 125th St / RFK Bridge landing
    (40.7810, -73.9400),  # East River off 96th St
    (40.7742, -73.9420),  # East River off 80th St
    (40.7590, -73.9535),  # East River off 59th St (Queensboro Bridge)
    (40.7495, -73.9645),  # East River off 42nd St (United Nations)
    (40.7425, -73.9695),  # East River off 34th St
    (40.7370, -73.9720),  # East River off 23rd St
    (40.7160, -73.9715),  # East River off Houston St
    (40.7090, -73.9945),  # East River by the Brooklyn Bridge
    (40.7058, -73.9990),  # East River by the Seaport
)

# Corridors that define East Side traffic even when a record's geometry is
# missing. Matched case-insensitively against the link or camera name. The feed
# abbreviates heavily and inconsistently, hence the alternations.
CORRIDOR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("FDR Drive", r"\bFDR\b"),
    ("Harlem River Drive", r"HARLEM\s*RIV"),
    ("Queensboro Bridge", r"(QUEENSBORO|QUEENSBRO|ED\s*KOCH|\bQBB\b|\b59\s*(TH)?\s*ST\s*BR)"),
    ("Queens-Midtown Tunnel", r"(QUEENS\s*[- ]?\s*MID|MIDTOWN\s*TUN|\bQMT\b)"),
    ("RFK Bridge", r"(\bRFK\b|TRIBORO|TRI[- ]?BOROUGH)"),
    ("Williamsburg Bridge", r"WILLIAMSBURG"),
    ("Manhattan Bridge", r"MANHATTAN\s*BR"),
    ("Brooklyn Bridge", r"BROOKLYN\s*BR"),
    ("East Side avenues", r"\b(1(ST)?|2(ND)?|3(RD)?|YORK|LEX(INGTON)?|PARK|MADISON)\s*AVE?\b"),
    ("East Side cross streets", r"\bE(AST)?\.?\s*(14|23|34|42|49|57|59|61|72|79|86|96|106|110|116|125)\s*(TH|ST|ND|RD)?\s*ST"),
)

_COMPILED_CORRIDORS = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in CORRIDOR_PATTERNS
)

# Corridors carrying motorway-grade traffic. Used to pick a sensible free-flow
# reference speed when there is no observed history to derive one from: NYC's
# default arterial limit is 25 mph, the FDR is posted at 40-50.
HIGHWAY_CORRIDORS = frozenset(
    {"FDR Drive", "Harlem River Drive", "RFK Bridge"}
)
CROSSING_CORRIDORS = frozenset(
    {
        "Queensboro Bridge",
        "Queens-Midtown Tunnel",
        "Williamsburg Bridge",
        "Manhattan Bridge",
        "Brooklyn Bridge",
    }
)

REFERENCE_SPEED_MPH = {
    "highway": 50.0,
    "crossing": 35.0,
    "arterial": 25.0,
}

EARTH_RADIUS_MILES = 3958.7613


@dataclass(frozen=True)
class Region:
    """A named area of interest: a polygon plus the corridor names inside it."""

    name: str
    polygon: tuple[tuple[float, float], ...]
    corridors: tuple[tuple[str, re.Pattern[str]], ...] = field(default=_COMPILED_CORRIDORS)

    def contains_point(self, lat: float | None, lon: float | None) -> bool:
        return point_in_polygon(lat, lon, self.polygon)

    def contains_any(self, points: Iterable[tuple[float, float]]) -> bool:
        return any(point_in_polygon(lat, lon, self.polygon) for lat, lon in points)

    def corridor_for(self, name: str | None) -> str | None:
        return corridor_for(name, self.corridors)

    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lat, min_lon, max_lat, max_lon) -- cheap prefilter before the polygon."""
        lats = [lat for lat, _ in self.polygon]
        lons = [lon for _, lon in self.polygon]
        return min(lats), min(lons), max(lats), max(lons)


EAST_SIDE = Region(name="Manhattan East Side", polygon=EAST_SIDE_POLYGON)


def point_in_polygon(
    lat: float | None,
    lon: float | None,
    polygon: Sequence[tuple[float, float]] = EAST_SIDE_POLYGON,
) -> bool:
    """Ray casting in lat/lon space.

    Over a two-mile-wide polygon the difference between planar and spherical
    geometry is far smaller than the polygon's own drafting error, so treating
    degrees as a plane is fine here.

    A point exactly on an edge is not guaranteed either way, which is the usual
    and accepted behaviour for this algorithm.
    """
    if lat is None or lon is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False

    inside = False
    count = len(polygon)
    j = count - 1
    for i in range(count):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        # Does the edge straddle the test latitude, and is the crossing east of
        # the test point?
        if (lat_i > lat) != (lat_j > lat):
            slope = (lon_j - lon_i) / (lat_j - lat_i)
            crossing_lon = lon_i + slope * (lat - lat_i)
            if lon < crossing_lon:
                inside = not inside
        j = i
    return inside


def corridor_for(
    name: str | None,
    patterns: Sequence[tuple[str, re.Pattern[str]]] = _COMPILED_CORRIDORS,
) -> str | None:
    """First matching corridor label for a link or camera name, else None."""
    if not name:
        return None
    for label, pattern in patterns:
        if pattern.search(name):
            return label
    return None


def road_class(corridor: str | None) -> str:
    """Bucket a corridor into highway / crossing / arterial."""
    if corridor in HIGHWAY_CORRIDORS:
        return "highway"
    if corridor in CROSSING_CORRIDORS:
        return "crossing"
    return "arterial"


def default_reference_speed(corridor: str | None) -> float:
    """Assumed free-flow speed, in mph, when no observed baseline exists."""
    return REFERENCE_SPEED_MPH[road_class(corridor)]


def parse_link_points(raw: str | None) -> list[tuple[float, float]]:
    """Parse the speed feed's ``link_points`` polyline into (lat, lon) pairs.

    The published format is a run of ``lat,lon`` pairs separated by spaces, but
    the live feed is untidy: pairs can be separated by spaces or by further
    commas, coordinates arrive with trailing spaces, the string can end on a
    lone separator, and a handful of rows carry a truncated final pair. Anything
    that does not parse as a plausible New York coordinate is dropped rather
    than raised, because one bad vertex should not discard a whole road segment.
    """
    if not raw:
        return []

    numbers: list[float] = []
    for token in re.split(r"[,\s]+", str(raw).strip()):
        if not token:
            continue
        try:
            numbers.append(float(token))
        except ValueError:
            continue

    points: list[tuple[float, float]] = []
    # A trailing unpaired coordinate is discarded by the floor division.
    for index in range(len(numbers) // 2):
        lat = numbers[index * 2]
        lon = numbers[index * 2 + 1]
        if is_plausible_nyc_coordinate(lat, lon):
            points.append((lat, lon))
    return points


def is_plausible_nyc_coordinate(lat: float | None, lon: float | None) -> bool:
    """Reject 0/0, swapped lat/lon, and anything outside the metropolitan area."""
    if lat is None or lon is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    return 40.0 <= lat <= 41.2 and -74.6 <= lon <= -73.3


def haversine_miles(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in statute miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def polyline_midpoint(
    points: Sequence[tuple[float, float]]
) -> tuple[float | None, float | None]:
    """Representative point for a link: the vertex nearest its centroid.

    The arithmetic centroid of a curved road can sit off the road itself (think
    of the FDR rounding the bend at Corlears Hook), which would then pick the
    wrong nearest camera. Snapping back to a real vertex keeps the point on the
    roadway.
    """
    if not points:
        return None, None
    mean_lat = sum(lat for lat, _ in points) / len(points)
    mean_lon = sum(lon for _, lon in points) / len(points)
    best = min(
        points,
        key=lambda p: haversine_miles(p[0], p[1], mean_lat, mean_lon),
    )
    return best[0], best[1]

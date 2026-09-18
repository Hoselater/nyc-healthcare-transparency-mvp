"""Turn a snapshot of link speeds into a readable picture of traffic.

The feed gives a current speed per segment and nothing to compare it against,
so congestion has to be defined here. Two references are used, in order of
preference:

1. **Observed baseline.** If a history file holds enough past snapshots of a
   link, spread over a wide enough window, its 85th-percentile observed speed is
   used as that link's free-flow speed. This is the honest reference: it is what
   the road actually does when it is moving, measured by the same sensor, so
   sensor bias cancels out.

   The window matters as much as the count. A link observed thirty times, all of
   them between five and six on a weekday evening, has an 85th-percentile speed
   that *is* its congested speed, and comparing it against itself would report
   a jammed road as free-flowing. So a baseline is only trusted once the
   observations span :data:`MIN_HISTORY_SPAN_HOURS`, and one that still lands far
   below the posted limit is flagged rather than quietly believed.
2. **Posted-limit fallback.** With no history, a reference is assumed from the
   road class -- 50 mph for the FDR and Harlem River Drive, 35 for the East
   River crossings, 25 for arterials, New York City's default limit.

Every output row records which reference it used, because a congestion ratio
built on the fallback is a weaker claim than one built on observation.
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from etl.nycdot import geo
from etl.nycdot.cameras import Camera, nearest_cameras
from etl.nycdot.speeds import SpeedLink, feed_age_minutes, usable

log = logging.getLogger(__name__)

# Below this many past readings, a percentile is noise, so the fallback wins.
MIN_HISTORY_OBSERVATIONS = 12
# ... and below this span, the readings all describe the same traffic conditions.
MIN_HISTORY_SPAN_HOURS = 6.0
BASELINE_PERCENTILE = 85
# An observed baseline this far under the posted-limit assumption suggests the
# collection window never caught the road flowing freely.
SUSPICIOUS_BASELINE_FRACTION = 0.5

# Ratio of current speed to free-flow speed.
CONGESTION_BANDS: tuple[tuple[str, float], ...] = (
    ("free flow", 0.80),
    ("moderate", 0.60),
    ("heavy", 0.40),
    ("severe", 0.0),
)


@dataclass
class LinkAssessment:
    """A link with its congestion verdict and the cameras that overlook it."""

    link: SpeedLink
    reference_speed_mph: float
    reference_source: str
    congestion_ratio: float
    congestion_level: str
    delay_seconds: float | None
    cameras: list[tuple[Camera, float]]

    def as_row(self) -> dict[str, Any]:
        row = self.link.as_row()
        row.update(
            {
                "reference_speed_mph": self.reference_speed_mph,
                "reference_source": self.reference_source,
                "congestion_ratio": self.congestion_ratio,
                "congestion_level": self.congestion_level,
                "delay_seconds_vs_free_flow": self.delay_seconds,
                "nearest_camera_id": self.cameras[0][0].camera_id if self.cameras else None,
                "nearest_camera_name": self.cameras[0][0].name if self.cameras else None,
                "nearest_camera_miles": round(self.cameras[0][1], 2) if self.cameras else None,
                "nearest_camera_image_url": self.cameras[0][0].image_url if self.cameras else None,
            }
        )
        return row


def percentile(values: Sequence[float], percent: float) -> float | None:
    """Linear-interpolated percentile. Avoids a numpy import for one number."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (percent / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class Baseline:
    """An observed free-flow estimate for one link, with its provenance."""

    speed_mph: float
    observations: int
    span_hours: float
    suspicious: bool

    def describe(self) -> str:
        text = (
            f"observed p{BASELINE_PERCENTILE} of {self.observations} readings "
            f"over {self.span_hours:.1f}h"
        )
        if self.suspicious:
            text += "; far below the posted limit, the window may be all peak"
        return text


def _parse_observed_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def baselines_from_history(
    history_rows: Iterable[dict[str, Any]],
    *,
    min_observations: int = MIN_HISTORY_OBSERVATIONS,
    min_span_hours: float = MIN_HISTORY_SPAN_HOURS,
) -> dict[str, Baseline]:
    """Per-link free-flow speed, as the 85th percentile of past observations.

    Zero and stale readings are excluded: they describe a broken sensor, not a
    speed the road is capable of. A link is only given an observed baseline once
    it has both enough readings and a wide enough window, so that a short burst
    of collection during one jam cannot redefine that jam as normal.
    """
    speeds: dict[str, list[float]] = defaultdict(list)
    times: dict[str, list[datetime]] = defaultdict(list)
    corridors: dict[str, str | None] = {}

    for row in history_rows:
        link_id = str(row.get("link_id", "")).strip()
        if not link_id:
            continue
        if str(row.get("is_stale", "")).strip().lower() in {"true", "1"}:
            continue
        try:
            speed = float(row.get("speed_mph") or 0)
        except (TypeError, ValueError):
            continue
        if speed <= 0:
            continue
        speeds[link_id].append(speed)
        observed_at = _parse_observed_at(row.get("observed_at_utc"))
        if observed_at:
            times[link_id].append(observed_at)
        corridors.setdefault(link_id, row.get("corridor") or None)

    baselines: dict[str, Baseline] = {}
    too_few = too_narrow = 0
    for link_id, values in speeds.items():
        if len(values) < min_observations:
            too_few += 1
            continue
        stamps = times.get(link_id) or []
        span_hours = (
            (max(stamps) - min(stamps)).total_seconds() / 3600.0 if len(stamps) > 1 else 0.0
        )
        if span_hours < min_span_hours:
            too_narrow += 1
            continue
        estimate = percentile(values, BASELINE_PERCENTILE)
        if not estimate or estimate <= 0:
            continue
        assumed = geo.default_reference_speed(corridors.get(link_id))
        baselines[link_id] = Baseline(
            speed_mph=round(estimate, 1),
            observations=len(values),
            span_hours=round(span_hours, 1),
            suspicious=estimate < assumed * SUSPICIOUS_BASELINE_FRACTION,
        )

    log.info(
        "Observed baselines for %d links (%d had too few readings, %d too narrow "
        "a window; those fall back to the posted-limit assumption)",
        len(baselines),
        too_few,
        too_narrow,
    )
    return baselines


def classify(ratio: float) -> str:
    for label, floor in CONGESTION_BANDS:
        if ratio >= floor:
            return label
    return "severe"


def assess_links(
    links: Sequence[SpeedLink],
    cameras: Sequence[Camera] = (),
    baselines: dict[str, Baseline] | None = None,
) -> list[LinkAssessment]:
    """Attach a congestion verdict and nearby cameras to every usable link."""
    baselines = baselines or {}
    assessments: list[LinkAssessment] = []

    for link in usable(links):
        observed = baselines.get(link.link_id)
        if observed:
            reference = observed.speed_mph
            source = observed.describe()
        else:
            reference = geo.default_reference_speed(link.corridor)
            source = f"assumed for {link.road_class}"

        speed = link.speed_mph or 0.0
        # A link can exceed its own reference; the ratio is capped so one
        # speeding sensor cannot pull a corridor average above free flow.
        ratio = round(min(speed / reference, 1.5), 3)

        delay = None
        if link.length_miles and reference > 0:
            free_flow_seconds = (link.length_miles / reference) * 3600
            current_seconds = (link.length_miles / speed) * 3600 if speed > 0 else None
            if current_seconds is not None:
                delay = round(current_seconds - free_flow_seconds, 1)

        assessments.append(
            LinkAssessment(
                link=link,
                reference_speed_mph=reference,
                reference_source=source,
                congestion_ratio=ratio,
                congestion_level=classify(ratio),
                delay_seconds=delay,
                cameras=nearest_cameras(link.latitude, link.longitude, cameras),
            )
        )

    assessments.sort(key=lambda item: item.congestion_ratio)
    return assessments


def corridor_summary(assessments: Sequence[LinkAssessment]) -> list[dict[str, Any]]:
    """Roll links up to named corridors, weighting by segment length.

    A plain mean would let a 0.1-mile ramp count as much as three miles of the
    FDR. Weighting by length makes the corridor figure an estimate of the speed
    a driver actually experiences across it.
    """
    grouped: dict[str, list[LinkAssessment]] = defaultdict(list)
    for item in assessments:
        grouped[item.link.corridor or "Other East Side roads"].append(item)

    rows: list[dict[str, Any]] = []
    for corridor, items in grouped.items():
        speeds = [item.link.speed_mph or 0.0 for item in items]
        lengths = [item.link.length_miles or 0.0 for item in items]
        total_length = sum(lengths)

        if total_length > 0:
            weighted_speed = sum(
                (item.link.speed_mph or 0.0) * (item.link.length_miles or 0.0)
                for item in items
            ) / total_length
            weighted_ratio = sum(
                item.congestion_ratio * (item.link.length_miles or 0.0) for item in items
            ) / total_length
        else:
            weighted_speed = statistics.fmean(speeds) if speeds else 0.0
            weighted_ratio = (
                statistics.fmean([item.congestion_ratio for item in items]) if items else 0.0
            )

        worst = min(items, key=lambda item: item.congestion_ratio)
        rows.append(
            {
                "corridor": corridor,
                "links": len(items),
                "miles_covered": round(total_length, 2),
                "mean_speed_mph": round(weighted_speed, 1),
                "congestion_ratio": round(weighted_ratio, 3),
                "congestion_level": classify(weighted_ratio),
                "slowest_link": worst.link.link_name,
                "slowest_speed_mph": worst.link.speed_mph,
                "total_delay_seconds": round(
                    sum(item.delay_seconds or 0.0 for item in items), 1
                ),
            }
        )

    rows.sort(key=lambda row: row["congestion_ratio"])
    return rows


def snapshot_quality(links: Sequence[SpeedLink]) -> dict[str, Any]:
    """What the snapshot could and could not measure. Printed with every report."""
    region_links = [link for link in links if link.in_region]
    zero = [link for link in region_links if (link.speed_mph or 0) <= 0]
    stale = [link for link in region_links if link.is_stale]
    no_geometry = [link for link in region_links if link.point_count == 0]

    disagreeing = []
    for link in region_links:
        if link.implied_speed_mph and link.speed_mph and link.speed_mph > 0:
            ratio = link.implied_speed_mph / link.speed_mph
            if ratio > 2 or ratio < 0.5:
                disagreeing.append(link)

    ages = [link.age_minutes for link in region_links if link.age_minutes is not None]
    future = [age for age in ages if age < -5]
    # Measured across every link, not just this region: the publisher's lag is a
    # property of the feed, and a region with few sensors would misreport it.
    feed_age = feed_age_minutes(links)
    return {
        "links_citywide": len(links),
        "links_in_region": len(region_links),
        "links_used": len(usable(region_links)),
        "links_reporting_zero": len(zero),
        "links_stale": len(stale),
        "links_without_geometry": len(no_geometry),
        "links_speed_disagrees_with_travel_time": len(disagreeing),
        "links_timestamped_in_future": len(future),
        "feed_age_minutes": round(feed_age, 1) if feed_age is not None else None,
        "median_reading_age_minutes": round(statistics.median(ages), 1) if ages else None,
    }


def render_report(
    assessments: Sequence[LinkAssessment],
    corridors: Sequence[dict[str, Any]],
    quality: dict[str, Any],
    cameras: Sequence[Camera],
    *,
    region_name: str = geo.EAST_SIDE.name,
    generated_at: datetime | None = None,
    worst_n: int = 12,
) -> str:
    """A Markdown briefing: the headline, the corridors, the worst segments."""
    generated_at = generated_at or datetime.now(timezone.utc)
    local = generated_at.astimezone(_new_york())
    lines: list[str] = []

    lines.append(f"# {region_name} traffic snapshot")
    lines.append("")
    lines.append(
        f"Taken {local.strftime('%A %d %B %Y, %H:%M')} New York time "
        f"({generated_at.strftime('%H:%M')} UTC)."
    )
    lines.append("")

    feed_age = quality.get("feed_age_minutes")
    if feed_age is not None and feed_age > 60:
        # The distinction matters: this says nothing about traffic, only about
        # when the city last published.
        hours = feed_age / 60
        measured_at = (generated_at - timedelta(minutes=feed_age)).astimezone(_new_york())
        lines.append(
            f"> **The published feed is {hours:.1f} hours behind.** The newest "
            f"reading available was taken at {measured_at.strftime('%H:%M')} New "
            "York time, so everything below describes that moment, not now."
        )
        lines.append("")
    elif feed_age is not None:
        minutes = round(feed_age)
        lines.append(
            f"Readings are {minutes} minute{'' if minutes == 1 else 's'} old at most."
        )
        lines.append("")

    if not assessments:
        lines.append(
            "No usable link readings for this region in this snapshot. Every link "
            "was lagging the rest of the feed, reporting zero, or outside the "
            "area. Nothing can be said about traffic from it."
        )
        lines.append("")
    else:
        speeds = [item.link.speed_mph or 0 for item in assessments]
        overall_ratio = statistics.fmean([item.congestion_ratio for item in assessments])
        worst = assessments[0]
        lines.append("## Headline")
        lines.append("")
        observed = sum(
            1 for item in assessments if item.reference_source.startswith("observed")
        )
        basis = (
            "measured free-flow baselines"
            if observed == len(assessments)
            else "posted-limit assumptions"
            if observed == 0
            else f"measured baselines on {observed} of {len(assessments)} segments "
            "and posted-limit assumptions on the rest"
        )
        lines.append(
            f"Across {len(assessments)} measured segments the East Side is running "
            f"**{classify(overall_ratio)}**, averaging "
            f"{statistics.fmean(speeds):.1f} mph, which is {overall_ratio:.0%} of "
            f"free flow judged against {basis}."
        )
        lines.append("")
        lines.append(
            f"The slowest segment is **{worst.link.link_name or worst.link.link_id}** "
            f"at {worst.link.speed_mph:.1f} mph "
            f"({worst.congestion_ratio:.0%} of free flow)."
        )
        lines.append("")

    if corridors:
        lines.append("## Corridors, worst first")
        lines.append("")
        lines.append("| Corridor | Level | Mean speed (mph) | % of free flow | Links | Miles |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
        for row in corridors:
            lines.append(
                f"| {row['corridor']} | {row['congestion_level']} | "
                f"{row['mean_speed_mph']:.1f} | {row['congestion_ratio']:.0%} | "
                f"{row['links']} | {row['miles_covered']:.1f} |"
            )
        lines.append("")

    if assessments:
        lines.append(f"## Slowest {min(worst_n, len(assessments))} segments")
        lines.append("")
        lines.append("| Segment | Speed (mph) | % of free flow | Delay (s) | Camera |")
        lines.append("| --- | ---: | ---: | ---: | --- |")
        for item in assessments[:worst_n]:
            camera = item.cameras[0][0].name if item.cameras else "none within half a mile"
            delay = f"{item.delay_seconds:.0f}" if item.delay_seconds is not None else "n/a"
            lines.append(
                f"| {item.link.link_name or item.link.link_id} | "
                f"{item.link.speed_mph:.1f} | {item.congestion_ratio:.0%} | "
                f"{delay} | {camera} |"
            )
        lines.append("")

    online_cameras = [camera for camera in cameras if camera.in_region]
    lines.append("## Cameras in the area")
    lines.append("")
    lines.append(
        f"{len(online_cameras)} of {len(cameras)} public cameras fall inside the "
        f"{region_name} boundary"
        + (
            f", {sum(1 for camera in online_cameras if camera.is_online)} of them "
            "reporting online."
            if any(camera.is_online is not None for camera in online_cameras)
            else "."
        )
    )
    lines.append("")

    lines.append("## What this snapshot can and cannot say")
    lines.append("")
    lines.append("| Measure | Value |")
    lines.append("| --- | ---: |")
    for key, value in quality.items():
        lines.append(f"| {key.replace('_', ' ')} | {value if value is not None else 'n/a'} |")
    lines.append("")
    lines.append(
        "Sensor coverage is highways and major arterials only, so a street with "
        "no segment here is unmeasured, not clear. Readings of zero are treated "
        "as dropped sensors and excluded, as are sensors lagging more than a "
        "quarter of an hour behind the rest of the feed."
    )
    lines.append("")
    assumed = sum(
        1 for item in assessments if not item.reference_source.startswith("observed")
    )
    if assumed:
        lines.append(
            f"{assumed} of {len(assessments)} segments have no free-flow baseline "
            "measured from history yet, so their percentages are judged against the "
            "posted limit for the road class and are indicative rather than "
            "measured. Running 'python -m etl.nycdot watch' across a full day and a "
            "quiet night replaces those assumptions with observation."
        )
    else:
        lines.append(
            "Every segment is judged against a free-flow speed measured from its "
            "own history rather than an assumed limit."
        )
    lines.append("")
    return "\n".join(lines)


def _new_york():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # pragma: no cover - no tzdata
        return timezone.utc

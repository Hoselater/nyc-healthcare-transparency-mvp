"""Command line entry point: ``python -m etl.nycdot``.

    python -m etl.nycdot snapshot               one pull of both feeds, plus a report
    python -m etl.nycdot snapshot --images      also save a still from every camera
    python -m etl.nycdot cameras                the camera inventory only
    python -m etl.nycdot speeds                 the live link speeds only
    python -m etl.nycdot watch --interval 300   keep pulling, building a baseline
    python -m etl.nycdot report                 rebuild the report from history

Outputs go to ``exports/nycdot/`` (git-ignored). Nothing here needs a database
or an API key, though a free Socrata app token raises the rate limit.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from etl.nycdot import geo
from etl.nycdot.analyze import (
    assess_links,
    baselines_from_history,
    corridor_summary,
    render_report,
    snapshot_quality,
)
from etl.nycdot.cameras import CAMERA_LIST_URL, download_stills, fetch_cameras
from etl.nycdot.fetch import FeedUnavailable, build_session
from etl.nycdot.notify import (
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_THRESHOLD,
    STATE_FILENAME,
    Notifier,
    build_alerts,
    combine_alerts,
    load_state,
    reconcile_state,
    save_state,
)
from etl.nycdot.speeds import SPEEDS_URL, fetch_speeds

log = logging.getLogger("etl.nycdot")

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "exports" / "nycdot"
# History is partitioned by day and only ever appended to. One ever-growing
# file would be rewritten in full on every run, and a scheduled collector
# committing that to git stores a fresh copy of the whole thing every quarter of
# an hour. Day files append cleanly, compress against their own previous
# version, and let retention delete whole files instead of rewriting live ones.
HISTORY_DIRNAME = "history"
LEGACY_HISTORY_FILENAME = "speed_history.csv"


def write_csv(path: Path, rows: Sequence[dict[str, Any]], *, append: bool = False) -> Path:
    """Write rows to CSV, unioning keys so a new column never truncates a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        log.warning("Nothing to write to %s", path)
        return path

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    existing_header: list[str] | None = None
    if append and path.exists() and path.stat().st_size > 0:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            existing_header = next(reader, None)

    if existing_header:
        # Keep the file's own column order; anything new is dropped rather than
        # silently shifting every previous row by one column.
        dropped = [name for name in fieldnames if name not in existing_header]
        if dropped:
            log.warning(
                "%s already has a header without %s; those columns are not appended",
                path.name,
                ", ".join(dropped),
            )
        fieldnames = existing_header

    mode = "a" if append and existing_header else "w"
    with path.open(mode, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        writer.writerows(rows)

    log.info("%s %d rows to %s", "Appended" if mode == "a" else "Wrote", len(rows), path)
    return path


def history_file(output: Path, when: datetime | None = None) -> Path:
    """The day file the current run appends to."""
    when = when or datetime.now(timezone.utc)
    return output / HISTORY_DIRNAME / f"{when.strftime('%Y-%m-%d')}.csv"


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def prune_history(output: Path, days: float) -> int:
    """Delete history day files older than ``days``. Returns the number removed.

    Retention deletes whole files rather than rewriting live ones, so a run that
    dies midway can never leave a truncated history behind. Baselines only need
    a recent window anyway: a free-flow speed measured last spring describes a
    road that may since have been resurfaced or given a bus lane.
    """
    if days <= 0:
        return 0

    directory = output / HISTORY_DIRNAME
    if not directory.exists():
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    removed = 0
    for path in sorted(directory.glob("*.csv")):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            # Not a day file; leave anything unrecognised alone.
            continue
        if day < cutoff:
            path.unlink()
            removed += 1

    if removed:
        log.info("Pruned %d history day file(s) older than %g days", removed, days)
    return removed


def read_history(output: Path) -> list[dict[str, Any]]:
    """Every retained reading, day files plus any legacy single-file history."""
    rows = _read_csv(output / LEGACY_HISTORY_FILENAME)
    directory = output / HISTORY_DIRNAME
    if directory.exists():
        for path in sorted(directory.glob("*.csv")):
            rows.extend(_read_csv(path))
    return rows


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _region(args: argparse.Namespace) -> geo.Region | None:
    return None if getattr(args, "all_nyc", False) else geo.EAST_SIDE


def _report_url() -> str | None:
    """A link to the published report, when running somewhere that knows one.

    On GitHub Actions the server and repository are in the environment, so the
    alert can carry a tappable link straight to the latest report.
    """
    explicit = os.getenv("REPORT_URL")
    if explicit:
        return explicit
    server = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    branch = os.getenv("DATA_BRANCH")
    if server and repository and branch:
        return f"{server}/{repository}/blob/{branch}/traffic_data/latest_report.md"
    return None


def send_alerts(args: argparse.Namespace, corridors: Sequence[dict[str, Any]]) -> int:
    """Notify on corridors that have changed state. Returns alerts delivered."""
    state_path = args.output / STATE_FILENAME
    previous = load_state(state_path)

    corridor_filter = args.notify_corridors or os.getenv("NOTIFY_CORRIDORS") or ""
    alerts, tentative = build_alerts(
        corridors,
        previous,
        threshold=args.notify_level or os.getenv("NOTIFY_LEVEL") or DEFAULT_THRESHOLD,
        cooldown_minutes=args.notify_cooldown,
        only=[part for part in corridor_filter.split(",") if part.strip()] or None,
        report_url=_report_url(),
    )

    notifier = Notifier()
    if notifier.transport is None:
        print(
            "Alerts are on but no transport is configured. Set NTFY_TOPIC (or "
            "Pushover / Telegram credentials). See docs/NYCDOT_TRAFFIC.md.",
            file=sys.stderr,
        )
        # The observed levels are still worth recording, but nothing was sent,
        # so nothing may be marked as notified.
        save_state(
            state_path,
            reconcile_state(tentative, previous, [alert.corridor for alert in alerts]),
        )  # nothing was sent, so nothing may be marked notified
        return 0

    # A burst goes out as one message, but the state is still per corridor, so
    # a failed summary has to un-mark every corridor it covered.
    outgoing = combine_alerts(alerts, report_url=_report_url())
    failed_kinds = {alert.kind for alert in outgoing if not notifier.send(alert)}
    failed = [alert.corridor for alert in alerts if alert.kind in failed_kinds]
    save_state(state_path, reconcile_state(tentative, previous, failed))

    delivered = len(outgoing) - len(failed_kinds)
    if outgoing:
        print(
            f"{delivered} of {len(outgoing)} notification(s) delivered via "
            f"{notifier.transport}, covering {len(alerts)} corridor change(s)"
        )
    return delivered


def _app_token() -> str | None:
    return os.getenv("NYC_OPEN_DATA_APP_TOKEN") or os.getenv("SOCRATA_APP_TOKEN") or None


def command_cameras(args: argparse.Namespace) -> int:
    session = build_session()
    region = _region(args)
    cameras = fetch_cameras(session, url=args.cameras_url, region=region)
    selected = [camera for camera in cameras if camera.in_region] if region else cameras

    stamp = _timestamp()
    write_csv(args.output / f"cameras_{stamp}.csv", [camera.as_row() for camera in selected])

    if args.images:
        manifest = download_stills(
            None,
            selected,
            args.output / "stills" / stamp,
            limit=args.image_limit,
        )
        write_csv(args.output / "stills" / stamp / "manifest.csv", manifest)

    print(f"{len(selected)} cameras" + (f" in {region.name}" if region else " citywide"))
    for camera in selected[:20]:
        status = {True: "online", False: "offline", None: "unknown"}[camera.is_online]
        print(f"  {camera.name or camera.camera_id} [{status}] {camera.image_url}")
    if len(selected) > 20:
        print(f"  ... and {len(selected) - 20} more in the CSV")
    return 0


def _collect(args: argparse.Namespace, session, cameras=None):
    """One pull of both feeds, returning everything the report needs."""
    region = _region(args)
    links = fetch_speeds(
        session, url=args.speeds_url, app_token=_app_token(), region=region
    )
    if cameras is None:
        try:
            cameras = fetch_cameras(session, url=args.cameras_url, region=region)
        except FeedUnavailable as exc:
            # Speeds are the measurement; cameras are corroboration. Losing the
            # camera feed degrades the report rather than ending the run.
            log.warning("Camera feed unavailable, continuing without it: %s", exc)
            cameras = []
    region_links = [link for link in links if link.in_region] if region else links
    return links, region_links, cameras


def command_speeds(args: argparse.Namespace) -> int:
    session = build_session()
    links, region_links, _ = _collect(args, session, cameras=[])

    stamp = _timestamp()
    rows = [link.as_row() for link in region_links]
    write_csv(args.output / f"links_{stamp}.csv", rows)
    write_csv(history_file(args.output), rows, append=True)
    prune_history(args.output, args.history_days)

    quality = snapshot_quality(links)
    print(f"{len(region_links)} links in scope, {quality['links_used']} usable")
    for link in sorted(
        (link for link in region_links if link.speed_mph),
        key=lambda link: link.speed_mph or 0,
    )[:15]:
        print(f"  {link.speed_mph:5.1f} mph  {link.link_name or link.link_id}")
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    session = build_session()
    links, region_links, cameras = _collect(args, session)

    stamp = _timestamp()
    rows = [link.as_row() for link in region_links]
    write_csv(args.output / f"links_{stamp}.csv", rows)
    write_csv(history_file(args.output), rows, append=True)
    write_csv(
        args.output / f"cameras_{stamp}.csv",
        [camera.as_row() for camera in cameras if camera.in_region or not _region(args)],
    )
    prune_history(args.output, args.history_days)

    baselines = baselines_from_history(read_history(args.output))
    assessments = assess_links(region_links, cameras, baselines)
    corridors = corridor_summary(assessments)
    quality = snapshot_quality(links)

    write_csv(args.output / f"assessed_links_{stamp}.csv", [item.as_row() for item in assessments])
    write_csv(args.output / f"corridors_{stamp}.csv", corridors)

    if args.notify:
        send_alerts(args, corridors)

    region = _region(args)
    report = render_report(
        assessments,
        corridors,
        quality,
        cameras,
        region_name=region.name if region else "New York City",
    )
    report_path = args.output / f"report_{stamp}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    (args.output / "latest_report.md").write_text(report, encoding="utf-8")
    log.info("Wrote report to %s", report_path)

    if args.images:
        in_region_cameras = [camera for camera in cameras if camera.in_region]
        manifest = download_stills(
            None,
            in_region_cameras,
            args.output / "stills" / stamp,
            limit=args.image_limit,
        )
        write_csv(args.output / "stills" / stamp / "manifest.csv", manifest)

    print(report)
    return 0


def command_watch(args: argparse.Namespace) -> int:
    """Poll on an interval so the history file can grow an observed baseline.

    The feed updates about once a minute; polling faster than that only
    re-reads the same numbers, so the interval floor is 60 seconds.
    """
    interval = max(60, args.interval)
    deadline = time.monotonic() + args.duration if args.duration else None
    session = build_session()
    pulls = 0

    while True:
        started = time.monotonic()
        try:
            links, region_links, _ = _collect(args, session, cameras=[])
            rows = [link.as_row() for link in region_links]
            write_csv(history_file(args.output), rows, append=True)
            prune_history(args.output, args.history_days)
            pulls += 1
            speeds = [link.speed_mph for link in region_links if link.speed_mph]
            mean = sum(speeds) / len(speeds) if speeds else 0
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] pull {pulls}: "
                f"{len(rows)} links, mean {mean:.1f} mph"
            )
        except FeedUnavailable as exc:
            # One bad pull must not end a watch that is meant to run for hours.
            log.warning("Pull failed, will retry at the next interval: %s", exc)

        if deadline and time.monotonic() >= deadline:
            break
        sleep_for = interval - (time.monotonic() - started)
        if sleep_for > 0:
            try:
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                print("\nStopped.")
                break

    print(f"{pulls} pulls written to {args.output / HISTORY_DIRNAME}")
    return 0


def command_report(args: argparse.Namespace) -> int:
    """Rebuild a report from the most recent rows already in the history file."""
    history = read_history(args.output)
    if not history:
        print(
            f"No history under {args.output / HISTORY_DIRNAME}. Run "
            "'python -m etl.nycdot snapshot' first.",
            file=sys.stderr,
        )
        return 1

    from etl.nycdot.speeds import SpeedLink

    latest_observation = max(row.get("observed_at_utc", "") for row in history)
    # Timestamps have one-second resolution, so two pulls launched in the same
    # second would otherwise put every link into the snapshot twice and double
    # every count in the report.
    latest_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in history:
        if row.get("observed_at_utc") != latest_observation:
            continue
        link_id = str(row.get("link_id", ""))
        if link_id in seen:
            continue
        seen.add(link_id)
        latest_rows.append(row)

    def to_link(row: dict[str, Any]) -> SpeedLink:
        def number(key: str) -> float | None:
            try:
                return float(row[key]) if row.get(key) not in (None, "") else None
            except (TypeError, ValueError):
                return None

        return SpeedLink(
            link_id=row.get("link_id", ""),
            link_name=row.get("link_name") or None,
            speed_mph=number("speed_mph"),
            travel_time_seconds=number("travel_time_seconds"),
            status=row.get("status") or None,
            data_as_of_local=row.get("data_as_of_local") or None,
            age_minutes=number("age_minutes"),
            lag_minutes=number("lag_minutes"),
            is_stale=str(row.get("is_stale", "")).lower() in {"true", "1"},
            borough=row.get("borough") or None,
            owner=row.get("owner") or None,
            length_miles=number("length_miles"),
            implied_speed_mph=number("implied_speed_mph"),
            latitude=number("latitude"),
            longitude=number("longitude"),
            point_count=int(number("point_count") or 0),
            corridor=row.get("corridor") or None,
            road_class=row.get("road_class") or "arterial",
            in_region=str(row.get("in_region", "")).lower() in {"true", "1"},
            match_reason=row.get("match_reason") or None,
            observed_at_utc=row.get("observed_at_utc", ""),
        )

    links = [to_link(row) for row in latest_rows]
    baselines = baselines_from_history(history)
    assessments = assess_links(links, [], baselines)
    corridors = corridor_summary(assessments)
    report = render_report(assessments, corridors, snapshot_quality(links), [])
    (args.output / "latest_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m etl.nycdot",
        description="Scrape NYC DOT live link speeds and traffic cameras, "
        "focused on the East Side of Manhattan.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"where to write CSVs and reports (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--all-nyc",
        action="store_true",
        help="do not filter to the East Side; keep every link and camera",
    )
    parser.add_argument("--speeds-url", default=SPEEDS_URL, help=argparse.SUPPRESS)
    parser.add_argument("--cameras-url", default=CAMERA_LIST_URL, help=argparse.SUPPRESS)
    parser.add_argument(
        "--notify",
        action="store_true",
        help="push an alert when a corridor crosses into heavy or severe traffic",
    )
    parser.add_argument(
        "--notify-level",
        choices=("moderate", "heavy", "severe"),
        default=None,
        help=f"level that triggers an alert (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--notify-cooldown",
        type=float,
        default=DEFAULT_COOLDOWN_MINUTES,
        help="minutes before the same level can alert again",
    )
    parser.add_argument(
        "--notify-corridors",
        default=None,
        help="comma-separated corridors to alert on (default: all)",
    )
    parser.add_argument(
        "--history-days",
        type=float,
        default=0,
        help="drop history rows older than this many days (0 = keep everything)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")

    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in (
        ("snapshot", command_snapshot, "pull both feeds and write a report"),
        ("cameras", command_cameras, "pull the camera inventory"),
        ("speeds", command_speeds, "pull the live link speeds"),
        ("report", command_report, "rebuild a report from saved history"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        if name in {"snapshot", "cameras"}:
            sub.add_argument(
                "--images", action="store_true", help="save a still from every camera"
            )
            sub.add_argument(
                "--image-limit",
                type=int,
                default=None,
                help="stop after this many camera stills",
            )

    watch = subparsers.add_parser("watch", help="poll the speed feed on an interval")
    watch.set_defaults(handler=command_watch)
    watch.add_argument("--interval", type=int, default=300, help="seconds between pulls (min 60)")
    watch.add_argument(
        "--duration", type=int, default=0, help="stop after this many seconds (0 = forever)"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not hasattr(args, "images"):
        args.images = False
        args.image_limit = None

    try:
        return args.handler(args)
    except FeedUnavailable as exc:
        print(f"\nFeed unavailable: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

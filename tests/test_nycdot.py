"""Offline tests for the NYC DOT traffic scraper.

Run from the repository root:

    python -m unittest discover -s tests -t .

Nothing here touches the network. The cases are the ones that actually broke
during development: naive timestamps read as the wrong timezone, malformed
polylines, cameras at 0/0, and baselines derived from too narrow a window.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from etl.nycdot import geo
from etl.nycdot.analyze import (
    MIN_HISTORY_SPAN_HOURS,
    assess_links,
    baselines_from_history,
    classify,
    corridor_summary,
    percentile,
)
from etl.nycdot.cameras import normalise_camera, nearest_cameras, tag_region
from etl.nycdot.cli import history_file, prune_history, read_history, write_csv
from etl.nycdot.speeds import normalise_link, parse_feed_timestamp, usable


class TestGeography(unittest.TestCase):
    def test_east_side_landmarks_are_inside(self):
        for name, lat, lon in [
            ("FDR Drive at 96th St", 40.7855, -73.9430),
            ("Grand Central", 40.7527, -73.9772),
            ("Lexington Ave at 125th St", 40.8045, -73.9375),
            ("Lower East Side", 40.7180, -73.9840),
            ("United Nations", 40.7489, -73.9680),
        ]:
            with self.subTest(name):
                self.assertTrue(geo.point_in_polygon(lat, lon), name)

    def test_west_side_and_outer_boroughs_are_outside(self):
        for name, lat, lon in [
            ("Times Square", 40.7580, -73.9855),
            ("Henry Hudson Pkwy", 40.7950, -73.9760),
            ("Downtown Brooklyn", 40.6920, -73.9880),
            ("Long Island City", 40.7450, -73.9490),
            ("Harlem west of Fifth Ave", 40.8100, -73.9500),
        ]:
            with self.subTest(name):
                self.assertFalse(geo.point_in_polygon(lat, lon), name)

    def test_rubbish_coordinates_are_rejected(self):
        self.assertFalse(geo.point_in_polygon(None, None))
        self.assertFalse(geo.point_in_polygon(0.0, 0.0))
        self.assertFalse(geo.point_in_polygon(float("nan"), -73.97))
        self.assertFalse(geo.is_plausible_nyc_coordinate(-73.97, 40.75))  # swapped

    def test_polyline_parser_survives_a_malformed_feed(self):
        self.assertEqual(
            geo.parse_link_points("40.7855,-73.9430 40.7800,-73.9440"),
            [(40.7855, -73.9430), (40.7800, -73.9440)],
        )
        # Comma separated pairs, trailing separator, and a truncated final pair.
        self.assertEqual(
            geo.parse_link_points("40.7855,-73.9430, 40.7800,-73.9440, 40.77"),
            [(40.7855, -73.9430), (40.7800, -73.9440)],
        )
        self.assertEqual(geo.parse_link_points(""), [])
        self.assertEqual(geo.parse_link_points(None), [])
        # Out-of-area vertices are dropped, not raised on.
        self.assertEqual(geo.parse_link_points("0,0 40.75,-73.97"), [(40.75, -73.97)])

    def test_midpoint_stays_on_the_road(self):
        points = [(40.7855, -73.9430), (40.7800, -73.9440), (40.7742, -73.9445)]
        self.assertIn(geo.polyline_midpoint(points), [(lat, lon) for lat, lon in points])
        self.assertEqual(geo.polyline_midpoint([]), (None, None))

    def test_corridor_matching(self):
        self.assertEqual(geo.corridor_for("FDR Dr S B 96th St - 79th St"), "FDR Drive")
        self.assertEqual(geo.corridor_for("QMT WB Queens Portal"), "Queens-Midtown Tunnel")
        self.assertEqual(geo.corridor_for("Ed Koch Queensboro Br"), "Queensboro Bridge")
        self.assertIsNone(geo.corridor_for("Belt Pkwy E B"))
        self.assertIsNone(geo.corridor_for(None))

    def test_road_class_drives_the_assumed_speed(self):
        self.assertEqual(geo.default_reference_speed("FDR Drive"), 50.0)
        self.assertEqual(geo.default_reference_speed("Queensboro Bridge"), 35.0)
        self.assertEqual(geo.default_reference_speed(None), 25.0)


class TestTimestamps(unittest.TestCase):
    def test_naive_timestamps_are_read_as_new_york_time(self):
        parsed = parse_feed_timestamp("2026-09-17T18:03:31.000")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        # 18:03 Eastern in September is 22:03 UTC, not 18:03 UTC. Getting this
        # wrong makes every reading look four hours stale.
        self.assertEqual(parsed.hour, 22)

    def test_explicit_offsets_are_trusted(self):
        self.assertEqual(parse_feed_timestamp("2026-09-17T22:03:31Z").hour, 22)

    def test_unparseable_values_return_none(self):
        self.assertIsNone(parse_feed_timestamp("not a date"))
        self.assertIsNone(parse_feed_timestamp(""))
        self.assertIsNone(parse_feed_timestamp(None))


try:
    from zoneinfo import ZoneInfo

    NEW_YORK = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - no tzdata
    NEW_YORK = timezone(timedelta(hours=-4))


def _feed_time(minutes_ago: float = 2) -> str:
    """A timestamp in the shape the feed publishes: New York local, no offset.

    Built explicitly in New York time rather than from a naive ``datetime.now()``
    so the suite gives the same answer on a machine set to UTC as on one set to
    Eastern.
    """
    moment = datetime.now(NEW_YORK) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000")


def _link_record(**overrides):
    record = {
        "link_id": "1000",
        "speed": "8.2",
        "travel_time": "300",
        "status": "0",
        "data_as_of": _feed_time(),
        "link_points": "40.7855,-73.9430 40.7800,-73.9440 40.7742,-73.9445",
        "link_name": "FDR Dr S B 116th St - 96th St",
        "borough": "Manhattan",
        "owner": "NYC_DOT",
    }
    record.update(overrides)
    return record


class TestSpeedLinks(unittest.TestCase):
    def test_a_link_crossing_the_polygon_is_in_region(self):
        link = normalise_link(_link_record())
        self.assertTrue(link.in_region)
        self.assertEqual(link.match_reason, "polyline crosses polygon")
        self.assertEqual(link.corridor, "FDR Drive")
        self.assertEqual(link.road_class, "highway")
        self.assertGreater(link.length_miles, 0)

    def test_a_link_outside_the_polygon_is_excluded(self):
        link = normalise_link(
            _link_record(
                link_name="Belt Pkwy E B Bay Pkwy - Exit 6",
                link_points="40.5980,-73.9980 40.5990,-73.9880",
            )
        )
        self.assertFalse(link.in_region)

    def test_a_named_corridor_without_geometry_still_counts(self):
        link = normalise_link(
            _link_record(link_points="", link_name="Queens Midtown Tunnel EB")
        )
        self.assertTrue(link.in_region)
        self.assertIn("no geometry", link.match_reason)

    def test_a_record_without_a_link_id_is_dropped(self):
        self.assertIsNone(normalise_link({"speed": "20"}))

    def test_travel_time_gives_an_independent_speed_estimate(self):
        link = normalise_link(_link_record(travel_time="300"))
        self.assertIsNotNone(link.implied_speed_mph)

    def test_zero_and_stale_readings_are_not_usable(self):
        fresh = normalise_link(_link_record())
        zero = normalise_link(_link_record(link_id="2", speed="0"))
        stale = normalise_link(_link_record(link_id="3", data_as_of=_feed_time(240)))
        self.assertTrue(stale.is_stale)
        self.assertEqual([link.link_id for link in usable([fresh, zero, stale])], ["1000"])


class TestCameras(unittest.TestCase):
    def test_image_url_is_synthesised_when_absent(self):
        camera = normalise_camera({"id": "abc", "latitude": "40.78", "longitude": "-73.94"})
        self.assertTrue(camera.image_url.endswith("/api/cameras/abc/image"))

    def test_null_island_coordinates_are_discarded_but_the_camera_is_kept(self):
        camera = normalise_camera(
            {"id": "c7", "name": "Queens Midtown Tunnel", "latitude": "0", "longitude": "0"}
        )
        self.assertIsNone(camera.latitude)
        tagged = tag_region([camera])[0]
        self.assertTrue(tagged.in_region)  # rescued by its name
        self.assertIn("no coordinates", tagged.match_reason)

    def test_online_flag_accepts_every_shape_the_feed_uses(self):
        for value, expected in [("true", True), (True, True), ("false", False), ("", None)]:
            with self.subTest(value=value):
                camera = normalise_camera({"id": "x", "isOnline": value})
                self.assertEqual(camera.is_online, expected)

    def test_a_record_without_an_id_is_dropped(self):
        self.assertIsNone(normalise_camera({"name": "nameless"}))

    def test_nearest_camera_respects_the_distance_cap(self):
        near = normalise_camera(
            {"id": "near", "latitude": "40.7812", "longitude": "-73.9405"}
        )
        far = normalise_camera({"id": "far", "latitude": "40.7050", "longitude": "-74.0100"})
        matches = nearest_cameras(40.7855, -73.9430, [near, far])
        self.assertEqual([camera.camera_id for camera, _ in matches], ["near"])


class TestAnalysis(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertAlmostEqual(percentile([10, 20], 50), 15.0)
        self.assertIsNone(percentile([], 85))

    def test_congestion_bands(self):
        self.assertEqual(classify(0.95), "free flow")
        self.assertEqual(classify(0.70), "moderate")
        self.assertEqual(classify(0.50), "heavy")
        self.assertEqual(classify(0.10), "severe")

    def _history(self, span_hours, readings=24, speed=30.0):
        start = datetime.now(timezone.utc) - timedelta(hours=span_hours)
        step = timedelta(hours=span_hours / max(readings - 1, 1))
        return [
            {
                "link_id": "1000",
                "speed_mph": str(speed),
                "is_stale": "False",
                "corridor": "FDR Drive",
                "observed_at_utc": (start + step * index).isoformat(timespec="seconds"),
            }
            for index in range(readings)
        ]

    def test_a_narrow_window_does_not_earn_a_baseline(self):
        # Every reading taken inside one rush hour: the 85th percentile is the
        # congested speed, so trusting it would report a jam as free flow.
        self.assertEqual(baselines_from_history(self._history(0.5)), {})

    def test_a_wide_window_earns_a_baseline(self):
        baselines = baselines_from_history(self._history(MIN_HISTORY_SPAN_HOURS + 2))
        self.assertIn("1000", baselines)
        self.assertAlmostEqual(baselines["1000"].speed_mph, 30.0, places=1)
        self.assertFalse(baselines["1000"].suspicious)

    def test_a_baseline_far_under_the_posted_limit_is_flagged(self):
        baselines = baselines_from_history(self._history(12, speed=9.0))
        self.assertTrue(baselines["1000"].suspicious)
        self.assertIn("all peak", baselines["1000"].describe())

    def test_too_few_readings_do_not_earn_a_baseline(self):
        self.assertEqual(baselines_from_history(self._history(12, readings=4)), {})

    def test_zero_readings_never_enter_a_baseline(self):
        rows = self._history(12)
        for row in rows[:12]:
            row["speed_mph"] = "0"
        self.assertEqual(baselines_from_history(rows), {})

    def test_corridor_summary_weights_by_length(self):
        long_slow = normalise_link(
            _link_record(
                link_id="a",
                speed="10",
                link_points="40.7855,-73.9430 40.7742,-73.9445 40.7605,-73.9550",
            )
        )
        short_fast = normalise_link(
            _link_record(
                link_id="b", speed="50", link_points="40.7605,-73.9550 40.7600,-73.9555"
            )
        )
        rows = corridor_summary(assess_links([long_slow, short_fast]))
        fdr = next(row for row in rows if row["corridor"] == "FDR Drive")
        # An unweighted mean would be 30 mph; the long slow segment must dominate.
        self.assertLess(fdr["mean_speed_mph"], 15)

    def test_assessment_falls_back_to_the_posted_limit(self):
        assessments = assess_links([normalise_link(_link_record(speed="10"))])
        self.assertEqual(assessments[0].reference_speed_mph, 50.0)
        self.assertIn("assumed", assessments[0].reference_source)
        self.assertEqual(assessments[0].congestion_level, "severe")


class TestCsvWriting(unittest.TestCase):
    def test_appending_keeps_the_original_column_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.csv"
            write_csv(path, [{"a": 1, "b": 2}])
            # A later run offering an extra column must not shift existing rows.
            write_csv(path, [{"a": 3, "b": 4, "c": 5}], append=True)
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["a"] for row in rows], ["1", "3"])
            self.assertNotIn("c", rows[0])

    def test_retention_deletes_whole_day_files(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for days_ago in (0, 1, 30):
                day = now - timedelta(days=days_ago)
                write_csv(
                    history_file(output, day),
                    [{"link_id": "a", "observed_at_utc": day.isoformat()}],
                )
            self.assertEqual(prune_history(output, days=7), 1)
            remaining = sorted(p.name for p in (output / "history").glob("*.csv"))
            self.assertEqual(len(remaining), 2)
            self.assertNotIn((now - timedelta(days=30)).strftime("%Y-%m-%d.csv"), remaining)

    def test_retention_ignores_files_it_cannot_date(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_csv(output / "history" / "notes.csv", [{"link_id": "a"}])
            self.assertEqual(prune_history(output, days=1), 0)
            self.assertTrue((output / "history" / "notes.csv").exists())

    def test_retention_is_a_no_op_when_disabled_or_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_csv(history_file(output), [{"link_id": "a"}])
            self.assertEqual(prune_history(output, days=0), 0)
            self.assertEqual(prune_history(Path(directory) / "nowhere", days=7), 0)

    def test_history_reads_day_files_and_the_legacy_file_together(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            # A history written by an earlier version must not be orphaned.
            write_csv(output / "speed_history.csv", [{"link_id": "old"}])
            write_csv(history_file(output, now), [{"link_id": "today"}])
            write_csv(history_file(output, now - timedelta(days=1)), [{"link_id": "yesterday"}])
            self.assertEqual(
                sorted(row["link_id"] for row in read_history(output)),
                ["old", "today", "yesterday"],
            )

    def test_the_day_file_is_named_for_its_date(self):
        moment = datetime(2026, 9, 18, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(history_file(Path("/out"), moment).name, "2026-09-18.csv")

    def test_writing_no_rows_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.csv"
            write_csv(path, [])
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

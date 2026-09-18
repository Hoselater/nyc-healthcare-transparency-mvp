"""End-to-end test of the traffic scraper's command line, against a local server.

The unit tests cover parsing and analysis. This covers the wiring: that a run
actually fetches both feeds, filters to the region, writes every output file,
and produces a report. It serves fixtures from a thread-local HTTP server on the
loopback interface, so it needs no internet access.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from etl.nycdot.cli import main
from tests.test_nycdot import _feed_time

# A real-sized JPEG, and the small placeholder an offline camera returns instead.
FRAME = b"\xff\xd8\xff\xe0" + b"\x00" * 4000 + b"\xff\xd9"
PLACEHOLDER = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"


def _links() -> list[dict[str, str]]:
    return [
        {
            "link_id": "1000",
            "speed": "8.2",
            "travel_time": "300",
            "status": "0",
            "data_as_of": _feed_time(2),
            "link_points": "40.7855,-73.9430 40.7800,-73.9440 40.7742,-73.9445",
            "link_name": "FDR Dr S B 116th St - 96th St",
            "borough": "Manhattan",
        },
        {
            "link_id": "1001",
            "speed": "31.0",
            "travel_time": "120",
            "status": "0",
            "data_as_of": _feed_time(3),
            "link_points": "40.7565,-73.9530 40.7580,-73.9620",
            "link_name": "Ed Koch Queensboro Br Upper Level WB",
            "borough": "Manhattan",
        },
        {  # Brooklyn: must be filtered out of an East Side run.
            "link_id": "1002",
            "speed": "45.0",
            "travel_time": "100",
            "status": "0",
            "data_as_of": _feed_time(2),
            "link_points": "40.5980,-73.9980 40.5990,-73.9880",
            "link_name": "Belt Pkwy E B Bay Pkwy - Exit 6",
            "borough": "Brooklyn",
        },
        {  # A dropped sensor: reported, but never averaged.
            "link_id": "1003",
            "speed": "0",
            "travel_time": "0",
            "status": "0",
            "data_as_of": _feed_time(2),
            "link_points": "40.7700,-73.9500 40.7650,-73.9530",
            "link_name": "FDR Dr N B dropped sensor",
            "borough": "Manhattan",
        },
    ]


def _cameras(port: int) -> list[dict[str, object]]:
    return [
        {
            "id": "c-001",
            "name": "FDR Drive @ 96 St",
            "latitude": "40.7812",
            "longitude": "-73.9405",
            "isOnline": "true",
            "imageUrl": f"http://127.0.0.1:{port}/img/c-001.jpg",
        },
        {
            "id": "c-002",
            "name": "2 Ave @ 79 St",
            "latitude": "40.7735",
            "longitude": "-73.9540",
            "isOnline": "false",
            "imageUrl": f"http://127.0.0.1:{port}/img/c-002.jpg",
        },
        {  # Brooklyn: filtered out.
            "id": "c-003",
            "name": "Atlantic Av @ Flatbush",
            "latitude": "40.6840",
            "longitude": "-73.9770",
            "isOnline": "true",
            "imageUrl": f"http://127.0.0.1:{port}/img/c-003.jpg",
        },
        {"name": "camera with no id"},
    ]


class _Handler(BaseHTTPRequestHandler):
    links: list[dict[str, str]] = []
    cameras: list[dict[str, object]] = []

    def log_message(self, *args):  # keep the test output quiet
        pass

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        if path == "/speeds.json":
            body, ctype = json.dumps(self.links).encode(), "application/json"
        elif path == "/cameras.json":
            body, ctype = json.dumps(self.cameras).encode(), "application/json"
        elif path.endswith(".jpg"):
            # c-002 is the offline camera, and answers with a placeholder.
            body = PLACEHOLDER if "c-002" in path else FRAME
            ctype = "image/jpeg"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestSnapshotCommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        _Handler.links = _links()
        _Handler.cameras = _cameras(cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _run(self, *extra: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        base = f"http://127.0.0.1:{self.port}"
        # The snapshot command prints the whole report; swallow it so the test
        # output stays readable.
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "--output",
                    str(directory),
                    "--speeds-url",
                    f"{base}/speeds.json",
                    "--cameras-url",
                    f"{base}/cameras.json",
                    *extra,
                ]
            )
        self.assertEqual(code, 0)
        return directory

    def _rows(self, directory: Path, prefix: str) -> list[dict[str, str]]:
        matches = sorted(directory.glob(f"{prefix}*.csv"))
        self.assertTrue(matches, f"no {prefix}*.csv written")
        with matches[-1].open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_snapshot_writes_every_output(self):
        directory = self._run("snapshot")
        for prefix in ("links_", "cameras_", "assessed_links_", "corridors_", "report_"):
            with self.subTest(prefix=prefix):
                self.assertTrue(list(directory.glob(f"{prefix}*")), prefix)
        self.assertTrue((directory / "latest_report.md").exists())
        self.assertTrue(list((directory / "history").glob("*.csv")))

    def test_snapshot_keeps_only_east_side_records(self):
        directory = self._run("snapshot")
        links = self._rows(directory, "links_")
        self.assertEqual(
            sorted(row["link_id"] for row in links), ["1000", "1001", "1003"]
        )
        cameras = self._rows(directory, "cameras_")
        self.assertEqual(sorted(row["camera_id"] for row in cameras), ["c-001", "c-002"])

    def test_dropped_sensors_are_reported_but_not_assessed(self):
        directory = self._run("snapshot")
        assessed = self._rows(directory, "assessed_links_")
        self.assertNotIn("1003", [row["link_id"] for row in assessed])
        report = (directory / "latest_report.md").read_text(encoding="utf-8")
        self.assertIn("links reporting zero | 1", report)

    def test_the_report_names_the_worst_segment_and_its_camera(self):
        directory = self._run("snapshot")
        report = (directory / "latest_report.md").read_text(encoding="utf-8")
        self.assertIn("FDR Dr S B 116th St - 96th St", report)
        self.assertIn("FDR Drive @ 96 St", report)
        self.assertIn("Manhattan East Side traffic snapshot", report)

    def test_all_nyc_disables_the_region_filter(self):
        directory = self._run("--all-nyc", "snapshot")
        links = self._rows(directory, "links_")
        self.assertIn("1002", [row["link_id"] for row in links])

    def test_images_are_saved_and_placeholders_rejected(self):
        directory = self._run("snapshot", "--images")
        manifests = list(directory.glob("stills/*/manifest.csv"))
        self.assertTrue(manifests)
        with manifests[0].open(encoding="utf-8", newline="") as handle:
            rows = {row["camera_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(rows["c-001"]["ok"], "True")
        self.assertEqual(rows["c-002"]["ok"], "False")
        self.assertIn("placeholder", rows["c-002"]["error"])
        self.assertTrue((Path(rows["c-001"]["path"])).exists())

    def test_report_command_rebuilds_from_history(self):
        directory = self._run("snapshot")
        (directory / "latest_report.md").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--output", str(directory), "report"]), 0)
        self.assertIn(
            "traffic snapshot",
            (directory / "latest_report.md").read_text(encoding="utf-8"),
        )

    def test_an_unreachable_feed_exits_cleanly(self):
        directory = Path(tempfile.mkdtemp())
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(
                [
                    "--output",
                    str(directory),
                    "--speeds-url",
                    f"http://127.0.0.1:{self.port}/nothing-here.json",
                    "speeds",
                ]
            )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

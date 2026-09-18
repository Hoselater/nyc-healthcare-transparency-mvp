"""Tests for phone alerts: when they fire, and what goes on the wire.

The alerting rule matters more than the transport. A collector that runs every
twenty minutes must not send an alert every twenty minutes, so most of these
tests are about staying silent.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from etl.nycdot.notify import (
    Alert,
    Notifier,
    build_alerts,
    combine_alerts,
    load_state,
    reconcile_state,
    save_state,
)


def corridor(name="FDR Drive", level="severe", speed=8.0, ratio=0.16):
    return {
        "corridor": name,
        "congestion_level": level,
        "mean_speed_mph": speed,
        "congestion_ratio": ratio,
        "slowest_link": "FDR Dr S B 116th St - 96th St",
    }


class TestAlertRules(unittest.TestCase):
    def test_first_severe_reading_alerts(self):
        alerts, state = build_alerts([corridor()], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, "worsened")
        self.assertIn("FDR Drive", alerts[0].title)
        self.assertEqual(state["FDR Drive"]["notified_level"], "severe")

    def test_an_ongoing_jam_does_not_re_alert(self):
        _, state = build_alerts([corridor()], {})
        # Twenty minutes later, still severe. Silence.
        alerts, _ = build_alerts([corridor()], state)
        self.assertEqual(alerts, [])

    def test_worsening_alerts_again_even_inside_the_cooldown(self):
        _, state = build_alerts([corridor(level="heavy")], {}, threshold="heavy")
        alerts, _ = build_alerts([corridor(level="severe")], state, threshold="heavy")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "severe")

    def test_improving_within_the_threshold_stays_quiet(self):
        _, state = build_alerts([corridor(level="severe")], {}, threshold="heavy")
        # Severe to heavy is still bad; no news.
        alerts, _ = build_alerts([corridor(level="heavy")], state, threshold="heavy")
        self.assertEqual(alerts, [])

    def test_clearing_alerts_once_and_then_stays_quiet(self):
        now = datetime.now(timezone.utc)
        _, state = build_alerts([corridor()], {}, now=now)

        # Better, but only just: too early to call it.
        alerts, state = build_alerts(
            [corridor(level="moderate", speed=22)], state, now=now + timedelta(minutes=20)
        )
        self.assertEqual(alerts, [])

        # Still better half an hour on. Now it is worth saying.
        alerts, state = build_alerts(
            [corridor(level="moderate", speed=22)], state, now=now + timedelta(minutes=55)
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, "cleared")
        self.assertIsNone(state["FDR Drive"]["notified_level"])

        alerts, _ = build_alerts(
            [corridor(level="moderate")], state, now=now + timedelta(minutes=75)
        )
        self.assertEqual(alerts, [])

    def test_a_single_good_reading_does_not_send_an_all_clear(self):
        now = datetime.now(timezone.utc)
        _, state = build_alerts([corridor()], {}, now=now)
        # One reading dips, then the jam is back. Saying "clearing" into a jam
        # that is still there is worse than saying nothing.
        alerts, state = build_alerts(
            [corridor(level="moderate")], state, now=now + timedelta(minutes=20)
        )
        self.assertEqual(alerts, [])
        alerts, state = build_alerts([corridor()], state, now=now + timedelta(minutes=40))
        self.assertEqual(alerts, [])
        self.assertEqual(state["FDR Drive"]["notified_level"], "severe")

    def test_nothing_below_the_threshold_ever_alerts(self):
        alerts, _ = build_alerts([corridor(level="heavy")], {}, threshold="severe")
        self.assertEqual(alerts, [])

    def test_cooldown_suppresses_a_flapping_corridor(self):
        now = datetime.now(timezone.utc)
        _, state = build_alerts([corridor()], {}, now=now)
        # Drops out and comes back inside the cooldown window.
        _, state = build_alerts([corridor(level="moderate")], state, now=now + timedelta(minutes=20))
        alerts, _ = build_alerts(
            [corridor()], state, now=now + timedelta(minutes=40), cooldown_minutes=90
        )
        self.assertEqual(alerts, [])

    def test_the_same_corridor_alerts_again_after_the_cooldown(self):
        now = datetime.now(timezone.utc)
        _, state = build_alerts([corridor()], {}, now=now)
        # It clears properly...
        for minutes in (20, 55):
            _, state = build_alerts(
                [corridor(level="moderate")], state, now=now + timedelta(minutes=minutes)
            )
        # ...and hours later it goes bad again, which is news.
        alerts, _ = build_alerts(
            [corridor()], state, now=now + timedelta(hours=4), cooldown_minutes=90
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, "worsened")

    def test_the_corridor_filter_is_respected(self):
        rows = [corridor("FDR Drive"), corridor("Queensboro Bridge")]
        alerts, _ = build_alerts(rows, {}, only=["fdr drive"])
        self.assertEqual([alert.corridor for alert in alerts], ["FDR Drive"])

    def test_rows_without_a_corridor_name_are_skipped(self):
        alerts, state = build_alerts([{"congestion_level": "severe"}], {})
        self.assertEqual(alerts, [])
        self.assertEqual(state, {})

    def test_the_message_carries_the_numbers_and_a_link(self):
        alerts, _ = build_alerts([corridor()], {}, report_url="https://example.invalid/r.md")
        message = alerts[0].message
        self.assertIn("8 mph", message)
        self.assertIn("16% of free flow", message)
        self.assertIn("https://example.invalid/r.md", message)

    def test_severe_outranks_heavy_in_priority(self):
        severe, _ = build_alerts([corridor(level="severe")], {}, threshold="heavy")
        heavy, _ = build_alerts([corridor(level="heavy")], {}, threshold="heavy")
        self.assertGreater(severe[0].priority, heavy[0].priority)


class TestFeedOutage(unittest.TestCase):
    """A feed that goes dark must never be reported as traffic clearing.

    The published feed has been observed answering a valid query with nothing.
    If that produced an all-clear, the phone would announce the jam was over
    while it was still there, purely because the city stopped answering.
    """

    def test_an_outage_is_silence_not_an_all_clear(self):
        now = datetime.now(timezone.utc)
        jam = [corridor(level="severe")]
        alerts, state = build_alerts(jam, {}, now=now)
        self.assertEqual(len(alerts), 1)

        # An hour of the feed returning nothing at all.
        for minutes in (20, 40, 60, 80):
            alerts, state = build_alerts([], state, now=now + timedelta(minutes=minutes))
            self.assertEqual(alerts, [], "an outage must not alert")

        # The jam is still remembered, so its return is not re-announced...
        self.assertEqual(state["FDR Drive"]["notified_level"], "severe")
        alerts, state = build_alerts(jam, state, now=now + timedelta(minutes=100))
        self.assertEqual(alerts, [])

        # ...and only a real, sustained recovery produces the all-clear.
        first, state = build_alerts(
            [corridor(level="free flow", speed=48, ratio=0.96)],
            state,
            now=now + timedelta(minutes=120),
        )
        self.assertEqual(first, [])
        second, state = build_alerts(
            [corridor(level="free flow", speed=48, ratio=0.96)],
            state,
            now=now + timedelta(minutes=155),
        )
        self.assertEqual([alert.kind for alert in second], ["cleared"])


class TestAlertVolume(unittest.TestCase):
    """A day of collection must produce a handful of alerts, not seventy."""

    def _day(self):
        """Seventy-two snapshots at twenty-minute intervals, with two rushes.

        Includes deliberate flapping at the edges of each rush, which is where
        a naive rule sends an alert on every single run.
        """
        start = datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc)
        flap = ["severe", "moderate", "severe", "heavy", "severe"]
        for index in range(72):
            moment = start + timedelta(minutes=20 * index)
            hour = moment.hour
            if 12 <= hour < 14 or 21 <= hour < 23:  # the two rushes, in UTC
                level = flap[index % len(flap)]
            else:
                level = "free flow"
            yield moment, level

    def test_a_full_day_sends_only_a_few_alerts(self):
        state: dict = {}
        alerts_sent = []
        for moment, level in self._day():
            alerts, state = build_alerts(
                [corridor(level=level)], state, now=moment, cooldown_minutes=90
            )
            alerts_sent.extend(alerts)

        # Two rush periods, each worth roughly one warning and one all-clear.
        self.assertLessEqual(len(alerts_sent), 8, [a.title for a in alerts_sent])
        self.assertGreaterEqual(len(alerts_sent), 2, "a bad day should say something")
        self.assertTrue(any(alert.kind == "worsened" for alert in alerts_sent))
        self.assertTrue(any(alert.kind == "cleared" for alert in alerts_sent))

    def test_a_quiet_day_sends_nothing(self):
        state: dict = {}
        for index in range(72):
            alerts, state = build_alerts(
                [corridor(level="free flow", speed=48, ratio=0.96)],
                state,
                now=datetime(2026, 9, 17, tzinfo=timezone.utc) + timedelta(minutes=20 * index),
            )
            self.assertEqual(alerts, [])


class TestBurstCombining(unittest.TestCase):
    def _alerts(self, count, kind="worsened", level="severe"):
        return [
            Alert(
                corridor=f"Corridor {index}",
                kind=kind,
                level=level,
                title=f"Corridor {index}: {level}",
                message="8 mph.",
                priority=5 if level == "severe" else 4,
            )
            for index in range(count)
        ]

    def test_a_small_number_goes_out_individually(self):
        alerts = self._alerts(2)
        self.assertEqual(len(combine_alerts(alerts)), 2)

    def test_a_burst_becomes_one_summary(self):
        combined = combine_alerts(self._alerts(4))
        self.assertEqual(len(combined), 1)
        self.assertIn("4 corridors severe", combined[0].title)
        for index in range(4):
            self.assertIn(f"Corridor {index}", combined[0].message)

    def test_warnings_and_all_clears_are_summarised_separately(self):
        alerts = self._alerts(3) + self._alerts(3, kind="cleared", level="moderate")
        combined = combine_alerts(alerts)
        self.assertEqual(len(combined), 2)
        kinds = sorted(alert.kind for alert in combined)
        self.assertEqual(kinds, ["cleared", "worsened"])

    def test_the_summary_takes_the_worst_priority(self):
        alerts = self._alerts(2, level="heavy") + self._alerts(2, level="severe")
        combined = combine_alerts(alerts)
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0].priority, 5)
        self.assertIn("severe", combined[0].title)

    def test_an_all_clear_summary_is_low_priority(self):
        combined = combine_alerts(self._alerts(4, kind="cleared", level="free flow"))
        self.assertEqual(combined[0].priority, 2)
        self.assertIn("clearing", combined[0].title)

    def test_the_report_link_rides_along(self):
        combined = combine_alerts(self._alerts(4), report_url="https://example.invalid/r")
        self.assertIn("https://example.invalid/r", combined[0].message)


class TestState(unittest.TestCase):
    def test_state_survives_a_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alert_state.json"
            save_state(path, {"FDR Drive": {"level": "severe"}})
            self.assertEqual(load_state(path)["FDR Drive"]["level"], "severe")

    def test_a_corrupt_state_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alert_state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_state(path), {})

    def test_a_missing_state_file_reads_as_empty(self):
        self.assertEqual(load_state(Path("/nonexistent/alert_state.json")), {})

    def test_a_failed_send_is_not_recorded_as_notified(self):
        previous = {}
        alerts, tentative = build_alerts([corridor()], previous)
        self.assertEqual(tentative["FDR Drive"]["notified_level"], "severe")

        # The send failed, so the next run has to try again.
        reconciled = reconcile_state(tentative, previous, ["FDR Drive"])
        self.assertIsNone(reconciled["FDR Drive"]["notified_level"])
        self.assertEqual(reconciled["FDR Drive"]["level"], "severe")

        alerts, _ = build_alerts([corridor()], reconciled)
        self.assertEqual(len(alerts), 1)


class _Capture(BaseHTTPRequestHandler):
    received: list[dict] = []
    status = 200

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        _Capture.received.append(
            {
                "path": self.path,
                "body": self.rfile.read(length).decode("utf-8"),
                "headers": dict(self.headers),
            }
        )
        self.send_response(_Capture.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class TestTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Capture)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        _Capture.received = []
        _Capture.status = 200

    def _notifier(self, **overrides):
        settings = {
            "NTFY_TOPIC": "secret-topic",
            "NTFY_SERVER": f"http://127.0.0.1:{self.port}",
            # Explicitly empty so a developer's real environment cannot leak in.
            "PUSHOVER_USER_KEY": "",
            "PUSHOVER_APP_TOKEN": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "NTFY_TOKEN": "",
        }
        settings.update(overrides)
        return Notifier(**settings)

    def _alert(self):
        return Alert(
            corridor="FDR Drive",
            kind="worsened",
            level="severe",
            title="FDR Drive: severe",
            message="8 mph, 16% of free flow.",
            priority=5,
        )

    def test_ntfy_posts_to_the_topic_with_the_right_headers(self):
        notifier = self._notifier()
        self.assertEqual(notifier.transport, "ntfy")
        self.assertTrue(notifier.send(self._alert()))

        sent = _Capture.received[0]
        self.assertEqual(sent["path"], "/secret-topic")
        self.assertEqual(sent["body"], "8 mph, 16% of free flow.")
        self.assertEqual(sent["headers"]["Title"], "FDR Drive: severe")
        self.assertEqual(sent["headers"]["Priority"], "5")
        self.assertIn("rotating_light", sent["headers"]["Tags"])

    def test_a_server_error_is_reported_not_raised(self):
        _Capture.status = 500
        self.assertFalse(self._notifier().send(self._alert()))

    def test_an_unreachable_server_is_reported_not_raised(self):
        notifier = self._notifier(NTFY_SERVER="http://127.0.0.1:1")
        self.assertFalse(notifier.send(self._alert()))

    def test_no_configuration_means_no_transport_and_no_crash(self):
        notifier = self._notifier(NTFY_TOPIC="")
        self.assertIsNone(notifier.transport)
        self.assertFalse(notifier.send(self._alert()))

    def test_pushover_is_used_when_ntfy_is_absent(self):
        notifier = self._notifier(
            NTFY_TOPIC="", PUSHOVER_USER_KEY="u", PUSHOVER_APP_TOKEN="t"
        )
        self.assertEqual(notifier.transport, "pushover")

    def test_telegram_is_the_last_resort(self):
        notifier = self._notifier(
            NTFY_TOPIC="", TELEGRAM_BOT_TOKEN="b", TELEGRAM_CHAT_ID="c"
        )
        self.assertEqual(notifier.transport, "telegram")

    def test_send_all_counts_only_what_went_out(self):
        _Capture.status = 500
        self.assertEqual(self._notifier().send_all([self._alert(), self._alert()]), 0)


if __name__ == "__main__":
    unittest.main()

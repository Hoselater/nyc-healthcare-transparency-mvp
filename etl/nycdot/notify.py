"""Push alerts to a phone when East Side traffic actually changes.

The collector takes a snapshot every twenty minutes. Sending a notification
each time would be seventy-two a day, which is a thing people turn off within an
hour. So this sends on *state change* only: a corridor crossing into heavy or
severe, getting worse after that, or clearing again. A jam that persists for two
hours is one notification, not six.

State lives in a small JSON file beside the other outputs, so it survives
between runs the same way the history does. Without it every run would look like
the first one and every ongoing jam would re-alert forever.

Three transports are supported, chosen by whichever credentials are present:

* **ntfy** (default) needs no account at all: subscribe to a topic in the app,
  publish to the same topic. Topics are unauthenticated by default, so the topic
  name is the only secret; pick an unguessable one.
* **Pushover** needs a user key and an application token.
* **Telegram** needs a bot token and a chat id.

A failed notification never fails a collection run. The traffic data is the
product; the alert is a convenience, and a phone that is unreachable must not
cost you the snapshot.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from etl.nycdot.fetch import build_session

log = logging.getLogger(__name__)

STATE_FILENAME = "alert_state.json"

# Worst last, so a comparison of positions is a comparison of severity.
LEVEL_ORDER = ("free flow", "moderate", "heavy", "severe")

DEFAULT_THRESHOLD = "severe"
DEFAULT_COOLDOWN_MINUTES = 90
# How long a corridor must stay below the threshold before the all-clear is
# believed. Traffic dips for a single reading all the time, and an all-clear
# sent into a jam that is still there is worse than no message at all.
DEFAULT_CLEAR_AFTER_MINUTES = 30
# More alerts than this in a single run arrive as one summary instead. At rush
# hour half the East Side turns severe within the same snapshot, and four
# notifications arriving together is how people learn to silence an app.
MAX_INDIVIDUAL_ALERTS = 2

NTFY_DEFAULT_SERVER = "https://ntfy.sh"


def level_rank(level: str | None) -> int:
    try:
        return LEVEL_ORDER.index((level or "").strip().lower())
    except ValueError:
        return 0


@dataclass
class Alert:
    """One thing worth interrupting someone for."""

    corridor: str
    kind: str  # "worsened" or "cleared"
    level: str
    title: str
    message: str
    priority: int  # 1 (lowest) to 5 (urgent), mapped per transport

    def tags(self) -> list[str]:
        if self.kind == "cleared":
            return ["white_check_mark"]
        return ["rotating_light"] if self.level == "severe" else ["warning"]


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A corrupt state file must not stop collection. Starting from empty
        # costs at most one duplicate alert.
        log.warning("Could not read alert state at %s, starting fresh: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_alerts(
    corridors: Sequence[dict[str, Any]],
    state: dict[str, Any],
    *,
    threshold: str = DEFAULT_THRESHOLD,
    cooldown_minutes: float = DEFAULT_COOLDOWN_MINUTES,
    clear_after_minutes: float = DEFAULT_CLEAR_AFTER_MINUTES,
    only: Iterable[str] | None = None,
    report_url: str | None = None,
    now: datetime | None = None,
) -> tuple[list[Alert], dict[str, Any]]:
    """Decide what is worth sending, and return the state to save afterwards.

    An alert fires when a corridor first reaches the threshold, and again if it
    worsens beyond that. It fires once more when the corridor has stayed below
    the threshold long enough to believe it. Anything else, including a jam
    sitting at the same level for hours, is silence.
    """
    now = now or datetime.now(timezone.utc)
    threshold_rank = level_rank(threshold)
    wanted = {name.strip().lower() for name in only} if only else None

    alerts: list[Alert] = []
    updated: dict[str, Any] = dict(state)

    for row in corridors:
        corridor = str(row.get("corridor") or "").strip()
        if not corridor:
            continue
        if wanted and corridor.lower() not in wanted:
            continue

        level = str(row.get("congestion_level") or "").strip().lower()
        rank = level_rank(level)
        previous = state.get(corridor) or {}
        notified_rank = level_rank(previous.get("notified_level"))
        notified_at = _parse(previous.get("notified_at"))

        last_alert_at = _parse(previous.get("last_alert_at")) or notified_at
        below_since = _parse(previous.get("below_since"))

        entry: dict[str, Any] = {
            "level": level,
            "seen_at": now.isoformat(timespec="seconds"),
            "notified_level": previous.get("notified_level"),
            "notified_at": previous.get("notified_at"),
            "last_alert_at": previous.get("last_alert_at"),
            "below_since": previous.get("below_since"),
        }
        stamp = now.isoformat(timespec="seconds")

        if rank >= threshold_rank:
            # Two different things can happen at or above the threshold, and
            # only one of them is urgent enough to ignore the cooldown.
            escalating = notified_rank >= threshold_rank and rank > notified_rank
            new_episode = notified_rank < threshold_rank

            # The cooldown is measured from the last alert of any kind, not from
            # the last warning. Measuring it from the warning alone lets a
            # corridor that flaps either side of the threshold alert on every
            # single run, which is the spam this whole rule exists to prevent.
            cooling = (
                last_alert_at is not None
                and now - last_alert_at < timedelta(minutes=cooldown_minutes)
            )

            # It is bad again, so any recovery it was part-way through is over.
            entry["below_since"] = None

            if escalating or (new_episode and not cooling):
                alerts.append(_worsened_alert(corridor, row, level, report_url))
                entry["notified_level"] = level
                entry["notified_at"] = stamp
                entry["last_alert_at"] = stamp
        else:
            if below_since is None:
                below_since = now
                entry["below_since"] = stamp

            recovered_for = now - below_since
            if (
                notified_rank >= threshold_rank
                and recovered_for >= timedelta(minutes=clear_after_minutes)
            ):
                # It was bad enough to tell you about, and it has now been better
                # for long enough to believe. This is never held back by the
                # cooldown: an all-clear that arrives an hour late is useless.
                alerts.append(_cleared_alert(corridor, row, level, report_url))
                entry["notified_level"] = None
                entry["notified_at"] = None
                entry["last_alert_at"] = stamp

        updated[corridor] = entry

    return alerts, updated


def combine_alerts(
    alerts: Sequence[Alert],
    *,
    max_individual: int = MAX_INDIVIDUAL_ALERTS,
    report_url: str | None = None,
) -> list[Alert]:
    """Collapse a burst of alerts into one message per kind.

    Warnings and all-clears are summarised separately: a single notification
    mixing "three corridors are severe" with "one is clearing" reads as
    gibberish on a lock screen.
    """
    worsened = [alert for alert in alerts if alert.kind == "worsened"]
    cleared = [alert for alert in alerts if alert.kind == "cleared"]

    combined: list[Alert] = []
    for group, kind in ((worsened, "worsened"), (cleared, "cleared")):
        if len(group) <= max_individual:
            combined.extend(group)
            continue
        combined.append(_summary_alert(group, kind, report_url))
    return combined


def _summary_alert(group: Sequence[Alert], kind: str, report_url: str | None) -> Alert:
    names = ", ".join(sorted(alert.corridor for alert in group))
    if kind == "cleared":
        title = f"East Side: {len(group)} corridors clearing"
        priority = 2
        level = "clearing"
    else:
        worst = max(group, key=lambda alert: alert.priority)
        level = worst.level
        title = f"East Side: {len(group)} corridors {level}"
        priority = worst.priority

    message = names if not report_url else f"{names}. {report_url}"
    return Alert(
        corridor=names,
        kind=kind,
        level=level,
        title=title,
        message=message,
        priority=priority,
    )


def reconcile_state(
    tentative: dict[str, Any],
    previous: dict[str, Any],
    failed: Iterable[str],
) -> dict[str, Any]:
    """Undo the "notified" marks for alerts that never actually went out.

    ``build_alerts`` marks a corridor as notified on the assumption the send
    succeeds. When it does not, that mark would suppress the alert forever, so
    the corridor keeps the notification fields it had before and the next run
    tries again. The observed level is still recorded either way.
    """
    reconciled = dict(tentative)
    for corridor in failed:
        entry = dict(reconciled.get(corridor) or {})
        before = previous.get(corridor) or {}
        entry["notified_level"] = before.get("notified_level")
        entry["notified_at"] = before.get("notified_at")
        # An alert that never arrived must not start a cooldown either.
        entry["last_alert_at"] = before.get("last_alert_at")
        reconciled[corridor] = entry
    return reconciled


def _speed(row: dict[str, Any]) -> str:
    try:
        return f"{float(row.get('mean_speed_mph')):.0f} mph"
    except (TypeError, ValueError):
        return "unknown speed"


def _share(row: dict[str, Any]) -> str:
    try:
        return f"{float(row.get('congestion_ratio')):.0%} of free flow"
    except (TypeError, ValueError):
        return "share of free flow unknown"


def _worsened_alert(
    corridor: str, row: dict[str, Any], level: str, report_url: str | None
) -> Alert:
    lines = [f"{_speed(row)}, {_share(row)}."]
    slowest = row.get("slowest_link")
    if slowest:
        lines.append(f"Slowest: {slowest}.")
    if report_url:
        lines.append(report_url)
    return Alert(
        corridor=corridor,
        kind="worsened",
        level=level,
        title=f"{corridor}: {level}",
        message=" ".join(lines),
        priority=5 if level == "severe" else 4,
    )


def _cleared_alert(
    corridor: str, row: dict[str, Any], level: str, report_url: str | None
) -> Alert:
    message = f"Back to {level or 'normal'}, {_speed(row)}."
    if report_url:
        message = f"{message} {report_url}"
    return Alert(
        corridor=corridor,
        kind="cleared",
        level=level,
        title=f"{corridor}: clearing",
        message=message,
        priority=2,
    )


class Notifier:
    """Sends alerts through whichever transport is configured."""

    def __init__(self, session: requests.Session | None = None, **settings: str | None):
        self.session = session or build_session(retries=2)
        get = lambda key: settings.get(key) or os.getenv(key) or None  # noqa: E731

        self.ntfy_topic = get("NTFY_TOPIC")
        self.ntfy_server = (get("NTFY_SERVER") or NTFY_DEFAULT_SERVER).rstrip("/")
        self.ntfy_token = get("NTFY_TOKEN")

        self.pushover_user = get("PUSHOVER_USER_KEY")
        self.pushover_token = get("PUSHOVER_APP_TOKEN")

        self.telegram_token = get("TELEGRAM_BOT_TOKEN")
        self.telegram_chat = get("TELEGRAM_CHAT_ID")

    @property
    def transport(self) -> str | None:
        if self.ntfy_topic:
            return "ntfy"
        if self.pushover_user and self.pushover_token:
            return "pushover"
        if self.telegram_token and self.telegram_chat:
            return "telegram"
        return None

    def send(self, alert: Alert) -> bool:
        """Deliver one alert. Returns whether it went out."""
        transport = self.transport
        if transport is None:
            log.info("No notification transport configured; skipping %s", alert.title)
            return False
        try:
            if transport == "ntfy":
                self._send_ntfy(alert)
            elif transport == "pushover":
                self._send_pushover(alert)
            else:
                self._send_telegram(alert)
        except requests.RequestException as exc:
            # The snapshot is the product. An unreachable phone is not a reason
            # to lose it.
            log.warning("Could not send alert via %s: %s", transport, exc)
            return False
        log.info("Sent alert via %s: %s", transport, alert.title)
        return True

    def send_all(self, alerts: Sequence[Alert]) -> int:
        return sum(1 for alert in alerts if self.send(alert))

    def _send_ntfy(self, alert: Alert) -> None:
        headers = {
            "Title": alert.title,
            "Priority": str(alert.priority),
            "Tags": ",".join(alert.tags()),
        }
        if self.ntfy_token:
            headers["Authorization"] = f"Bearer {self.ntfy_token}"
        response = self.session.post(
            f"{self.ntfy_server}/{self.ntfy_topic}",
            data=alert.message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()

    def _send_pushover(self, alert: Alert) -> None:
        response = self.session.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": self.pushover_token,
                "user": self.pushover_user,
                "title": alert.title,
                "message": alert.message,
                # Pushover's scale is -2..2, and 2 demands acknowledgement,
                # which is more than a traffic jam deserves.
                "priority": 1 if alert.priority >= 4 else 0,
            },
            timeout=15,
        )
        response.raise_for_status()

    def _send_telegram(self, alert: Alert) -> None:
        response = self.session.post(
            f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
            data={
                "chat_id": self.telegram_chat,
                "text": f"{alert.title}\n{alert.message}",
                "disable_notification": alert.priority <= 2,
            },
            timeout=15,
        )
        response.raise_for_status()

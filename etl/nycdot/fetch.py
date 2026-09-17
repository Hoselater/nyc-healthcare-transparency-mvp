"""Shared HTTP plumbing for the NYC DOT feeds.

Both feeds are public and unauthenticated, but they are also both live
municipal endpoints that rate-limit, occasionally 502, and occasionally hang.
One retrying session, built once, is used by every caller.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 v2 and v1.26 keep Retry in different places.
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - very old urllib3
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "nyc-traffic-snapshot/1.0 (public-data research; contact via repository)"
)
DEFAULT_TIMEOUT = 30


class FeedUnavailable(RuntimeError):
    """A feed could not be reached or returned something unusable."""


def build_session(user_agent: str | None = None, retries: int = 3) -> requests.Session:
    """A session that retries idempotent failures with exponential backoff."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent
            or os.getenv("HTTP_USER_AGENT")
            or DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
    )
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> requests.Response:
    """GET with the failure modes turned into one explanatory exception."""
    try:
        response = session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.exceptions.ProxyError as exc:
        raise FeedUnavailable(
            f"{url} was refused by an outbound proxy. If you are running inside a "
            "sandbox with an egress allow-list, this host has to be added to it; "
            "the feed itself is public and needs no credentials."
        ) from exc
    except requests.exceptions.SSLError as exc:
        raise FeedUnavailable(
            f"TLS verification failed for {url}. If a corporate or sandbox proxy "
            "re-terminates TLS, point REQUESTS_CA_BUNDLE at its CA bundle rather "
            "than disabling verification."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise FeedUnavailable(f"{url} could not be reached: {exc}") from exc

    if response.status_code >= 400:
        raise FeedUnavailable(
            f"{url} returned HTTP {response.status_code}: "
            f"{response.text[:200].strip()!r}"
        )
    return response


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Any:
    """GET and decode JSON, with the body quoted when it is not JSON at all."""
    response = get(session, url, params=params, headers=headers, timeout=timeout)
    try:
        return response.json()
    except ValueError as exc:
        raise FeedUnavailable(
            f"{url} did not return JSON (content-type "
            f"{response.headers.get('Content-Type', 'unknown')!r}): "
            f"{response.text[:200].strip()!r}"
        ) from exc


def first_present(record: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """Value for the first candidate key present, matched case-insensitively.

    The two feeds disagree about naming -- the camera API is camelCase, the
    Socrata mirror is snake_case, and the raw DOT text feed is a third style
    again. Normalising on lookup keeps one parser serving all of them.
    """
    for key in candidates:
        if key in record:
            value = record[key]
            if value not in (None, ""):
                return value

    lowered = {str(key).lower(): value for key, value in record.items()}
    for key in candidates:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None

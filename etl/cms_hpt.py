"""Discovery of hospital machine-readable files via the cms-hpt.txt protocol."""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import config

log = logging.getLogger(__name__)

# CMS defines these keys in the CY2024 OPPS Final Rule. Hospitals are
# inconsistent about capitalisation and whitespace, so parsing is case-folded.
KEY_LOCATION = "location-name"
KEY_SOURCE_PAGE = "source-page-url"
KEY_MRF_URL = "mrf-url"
KEY_CONTACT_NAME = "contact-name"
KEY_CONTACT_EMAIL = "contact-email"


@dataclass
class MrfLocation:
    system_name: str
    location_name: str | None
    mrf_url: str
    source_page_url: str | None = None

    @property
    def label(self) -> str:
        return self.location_name or self.system_name


def _parse_line(line: str) -> tuple[str, str] | None:
    """Split one `key: value` line, case-insensitively.

    The original implementation tested `line.lower().startswith("mrf-url:")` but
    then split the ORIGINAL line on the lowercase literal. A file written as
    `MRF-URL: https://...` -- which is common -- passes the test, produces a
    one-element list from the split, and raises IndexError on [1], killing the
    crawl for that hospital.
    """
    if ":" not in line:
        return None
    key, _, value = line.partition(":")
    key = key.strip().lower()
    if not key or key.startswith("#"):
        return None
    return key, value.strip()


def parse_cms_hpt(text: str, base_url: str, system_name: str) -> list[MrfLocation]:
    """Extract every MRF location from a cms-hpt.txt body.

    Multi-campus systems publish sequential repeating blocks of attributes, one
    per operating certificate. Taking only the first mrf-url -- as the original
    script did with its `break` -- silently drops every campus but one, which for
    a system like NYU Langone or Mount Sinai loses most of the market.
    """
    locations: list[MrfLocation] = []
    current: dict[str, str] = {}

    def flush() -> None:
        url = current.get(KEY_MRF_URL)
        if url:
            locations.append(
                MrfLocation(
                    system_name=system_name,
                    location_name=current.get(KEY_LOCATION),
                    # MRF urls are sometimes published site-relative.
                    mrf_url=urljoin(base_url, url),
                    source_page_url=current.get(KEY_SOURCE_PAGE),
                )
            )
        current.clear()

    for raw_line in text.splitlines():
        parsed = _parse_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed

        # A repeated location-name or mrf-url starts the next block.
        if key in (KEY_LOCATION, KEY_MRF_URL) and key in current:
            flush()

        if key in (
            KEY_LOCATION,
            KEY_SOURCE_PAGE,
            KEY_MRF_URL,
            KEY_CONTACT_NAME,
            KEY_CONTACT_EMAIL,
        ):
            current[key] = value

    flush()
    return locations


_TLS_EXEMPT: set[str] = set()


def _tls_exempt(domain: str) -> bool:
    """Hosts explicitly allow-listed for unverified TLS in the target CSV."""
    host = urlparse(domain if urlparse(domain).scheme else f"https://{domain}").hostname or domain
    return host.lower().lstrip("www.") in _TLS_EXEMPT or host.lower() in _TLS_EXEMPT


def discover(domain: str, system_name: str) -> list[MrfLocation]:
    """Fetch and parse `<domain>/cms-hpt.txt`."""
    base = domain if urlparse(domain).scheme else f"https://{domain}"
    base = base.rstrip("/") + "/"
    txt_url = urljoin(base, "cms-hpt.txt")

    log.info("Querying %s", txt_url)
    try:
        response = requests.get(
            txt_url,
            headers=config.HTTP_HEADERS,
            timeout=config.HTTP_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.exceptions.SSLError as exc:
        # Some hospitals let their certificate expire (Montefiore) or serve one
        # for the wrong hostname (SUNY Downstate). The file itself is a public
        # regulatory disclosure containing no secrets, so retrying without
        # verification is defensible -- but only when explicitly opted in per
        # host, and it is logged loudly because it does drop authentication of
        # the server's identity.
        if not _tls_exempt(domain):
            log.warning("  TLS failure for %s (%s). Set allow_insecure_tls=yes in "
                        "%s to fetch anyway.", system_name,
                        str(exc)[:80], config.TARGET_HOSPITALS_CSV.name)
            return []
        log.warning("  INSECURE: %s has a bad certificate; fetching unverified "
                    "because it is explicitly allow-listed.", system_name)
        try:
            response = requests.get(
                txt_url, headers=config.HTTP_HEADERS, timeout=config.HTTP_TIMEOUT,
                allow_redirects=True, verify=False,
            )
            response.raise_for_status()
        except requests.RequestException as exc2:
            log.warning("  still failed for %s: %s", system_name, str(exc2)[:120])
            return []
    except requests.RequestException as exc:
        log.warning("  no cms-hpt.txt for %s: %s", system_name, exc)
        return []

    # A missing file often returns a styled 200 HTML 404 page rather than a 404.
    body = response.text
    if "<html" in body[:2000].lower():
        log.warning("  %s served HTML, not a cms-hpt.txt file", txt_url)
        return []

    locations = parse_cms_hpt(body, txt_url, system_name)
    if not locations:
        log.warning("  no mrf-url entries found in %s", txt_url)
    else:
        log.info("  found %s MRF location(s)", len(locations))
    return locations


def load_targets(path: Path | None = None) -> list[dict]:
    """Read the target hospital list.

    Kept in data/target_hospitals.csv rather than as a Python literal so the
    market can be widened without editing code, and so rows can carry a manual
    mrf_url for the ~40-70% of hospitals that never deployed cms-hpt.txt.
    """
    path = path or config.TARGET_HOSPITALS_CSV
    if not Path(path).exists():
        raise FileNotFoundError(f"Target hospital list not found: {path}")

    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = [
            {k.strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(handle)
        ]
    return [r for r in rows if r.get("system_name")]


def discover_all(targets: list[dict] | None = None) -> list[MrfLocation]:
    """Resolve every target to zero or more MRF locations."""
    targets = targets if targets is not None else load_targets()
    found: list[MrfLocation] = []

    _TLS_EXEMPT.clear()
    for t in targets:
        if str(t.get("allow_insecure_tls", "")).strip().lower() in ("yes", "true", "1"):
            dom = (t.get("domain") or "").strip().lower()
            if dom:
                _TLS_EXEMPT.add(dom.lstrip("www."))
    if _TLS_EXEMPT:
        log.warning("Unverified TLS allow-listed for: %s", ", ".join(sorted(_TLS_EXEMPT)))

    for index, target in enumerate(targets):
        if index:
            time.sleep(config.CRAWL_DELAY_SECONDS)
        name = target["system_name"]

        # A manually-sourced URL wins: it means someone already checked.
        manual = target.get("manual_mrf_url")
        if manual:
            log.info("Using manual MRF url for %s", name)
            found.append(
                MrfLocation(
                    system_name=name,
                    location_name=target.get("facility_name") or name,
                    mrf_url=manual,
                )
            )
            continue

        domain = target.get("domain")
        if not domain:
            log.warning("Skipping %s: no domain and no manual_mrf_url", name)
            continue

        found.extend(discover(domain, name))

    log.info("Discovered %s MRF location(s) across %s systems.", len(found), len(targets))
    return found

"""CMS Care Compare quality measures for hip and knee replacement.

This is the answer to the sharpest criticism of the project: that length of stay
is only a proxy for quality. CMS publishes the risk-standardised COMPLICATION
rate and 30-day READMISSION rate for elective primary total hip and knee
arthroplasty, per hospital, free. Those are real outcomes, not proxies.

Joined on the CMS Certification Number, which SPARCS does not carry, so the
facilities are matched by name within New York State -- a far easier problem
than the MRF crosswalk, because both sources use similar official naming.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import requests

import config
from etl import db

log = logging.getLogger(__name__)

API = "https://data.cms.gov/provider-data/api/1"

# Distribution ids are stable per dataset release; resolved at runtime from the
# metastore so a CMS refresh does not silently break the loader.
DATASETS = {
    "complications": "ynj2-r877",   # Complications and Deaths - Hospital
    "readmissions": "632h-zaca",    # Unplanned Hospital Visits - Hospital
    "general": "xubh-q36u",         # Hospital General Information
}

# The two measures that matter here. CMS measure ids.
MEASURES = {
    "COMP_HIP_KNEE": "complication_rate",
    "READM_30_HIP_KNEE": "readmission_rate",
}

TARGET_COLUMNS = [
    "cms_certification_number",
    "facility_name",
    "citytown",
    "state",
    "zip_code",
    "countyparish",
    "measure_id",
    "measure_name",
    "score",
    "denominator",
    "compared_to_national",
    "start_date",
    "end_date",
]


def _distribution_id(dataset_id: str) -> str | None:
    try:
        meta = requests.get(
            f"{API}/metastore/schemas/dataset/items/{dataset_id}",
            params={"show-reference-ids": "true"}, timeout=60,
        ).json()
    except Exception as exc:  # noqa: BLE001
        log.warning("  could not resolve %s: %s", dataset_id, exc)
        return None
    for dist in meta.get("distribution", []):
        if isinstance(dist, dict):
            ident = dist.get("identifier")
            if ident:
                return ident
        elif isinstance(dist, str) and len(dist) > 8:
            return dist
    return None


# The CMS datastore silently returns an EMPTY result set when limit exceeds its
# internal cap -- not an error, not a truncated page, just nothing. A limit of
# 2000 yields zero rows for a query that returns 163 at limit 500, which looks
# exactly like "this state has no data". Keep this at or below 500.
MAX_PAGE = 500


def _query(dist: str, conditions: list[tuple[str, str]], limit: int = MAX_PAGE) -> list[dict]:
    limit = min(limit, MAX_PAGE)
    params: dict[str, Any] = {"limit": limit, "offset": 0}
    for i, (prop, value) in enumerate(conditions):
        params[f"conditions[{i}][property]"] = prop
        params[f"conditions[{i}][value]"] = value
        params[f"conditions[{i}][operator]"] = "="
    out: list[dict] = []
    while True:
        data = requests.get(f"{API}/datastore/query/{dist}", params=params, timeout=90).json()
        rows = data.get("results", [])
        out.extend(rows)
        if len(rows) < limit:
            break
        params["offset"] += limit
    return out


def _num(value: Any) -> float | None:
    """CMS uses 'Not Available' and similar sentinels for suppressed cells."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or not any(ch.isdigit() for ch in text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _rows(records: Iterable[dict]) -> list[dict]:
    out = []
    for r in records:
        out.append({
            "cms_certification_number": (r.get("facility_id") or "").strip(),
            "facility_name": (r.get("facility_name") or "").strip(),
            "citytown": (r.get("citytown") or "").strip(),
            "state": (r.get("state") or "").strip(),
            "zip_code": (r.get("zip_code") or "").strip(),
            "countyparish": (r.get("countyparish") or "").strip(),
            "measure_id": (r.get("measure_id") or "").strip(),
            "measure_name": (r.get("measure_name") or "").strip()[:300],
            "score": _num(r.get("score")),
            "denominator": _num(r.get("denominator")),
            "compared_to_national": (r.get("compared_to_national") or "").strip()[:120],
            "start_date": (r.get("start_date") or "").strip()[:32],
            "end_date": (r.get("end_date") or "").strip()[:32],
        })
    return out


def fetch_quality(state: str = "NY", truncate_first: bool = True) -> int:
    """Load hip/knee complication and readmission measures for one state."""
    if truncate_first:
        db.truncate("stg_cms_quality")

    total = 0
    for label, dataset in (("complications", DATASETS["complications"]),
                           ("readmissions", DATASETS["readmissions"])):
        dist = _distribution_id(dataset)
        if not dist:
            log.warning("  no distribution for %s (%s)", label, dataset)
            continue
        for measure in MEASURES:
            try:
                records = _query(dist, [("state", state), ("measure_id", measure)])
            except Exception as exc:  # noqa: BLE001 - one measure must not kill the load
                log.warning("  %s/%s failed: %s", label, measure, str(exc)[:120])
                continue
            if not records:
                continue
            rows = [r for r in _rows(records) if r["cms_certification_number"]]
            inserted = db.copy_records("stg_cms_quality", TARGET_COLUMNS, rows)
            total += inserted
            log.info("  %s: %s rows for %s", label, inserted, measure)

    log.info("Inserted %s CMS quality rows into stg_cms_quality.", total)
    return total


def fetch_overall_rating(state: str = "NY") -> int:
    """CMS overall hospital star rating -- useful context, not part of the index."""
    dist = _distribution_id(DATASETS["general"])
    if not dist:
        return 0
    try:
        records = _query(dist, [("state", state)])
    except Exception as exc:  # noqa: BLE001
        log.warning("  overall rating fetch failed: %s", str(exc)[:120])
        return 0

    rows = [{
        "cms_certification_number": (r.get("facility_id") or "").strip(),
        "facility_name": (r.get("facility_name") or "").strip(),
        "citytown": (r.get("citytown") or "").strip(),
        "state": (r.get("state") or "").strip(),
        "zip_code": (r.get("zip_code") or "").strip(),
        "countyparish": (r.get("countyparish") or "").strip(),
        "measure_id": "CMS_OVERALL_RATING",
        "measure_name": "CMS overall hospital rating (1-5 stars)",
        "score": _num(r.get("hospital_overall_rating")),
        "denominator": None,
        "compared_to_national": (r.get("hospital_ownership") or "").strip()[:120],
        "start_date": "",
        "end_date": "",
    } for r in records]
    rows = [r for r in rows if r["cms_certification_number"] and r["score"] is not None]
    inserted = db.copy_records("stg_cms_quality", TARGET_COLUMNS, rows)
    log.info("  overall rating: %s facilities", inserted)
    return inserted

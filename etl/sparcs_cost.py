"""SPARCS Cost Transparency ingestion.

Facility-level median charge and cost per APR-DRG, keyed on PFI. Free, official,
and complete across every Article 28 facility -- which makes it the answer to
the CMS price-file coverage problem, where crawling reaches roughly a quarter of
the market and is blocked outright at several major systems.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sodapy import Socrata

import config
from etl import db

log = logging.getLogger(__name__)

TARGET_COLUMNS = [
    "pfi_number",
    "facility_name",
    "apr_drg_code",
    "apr_drg_description",
    "apr_severity_of_illness_code",
    "medical_surgical_code",
    "discharges",
    "mean_charge",
    "median_charge",
    "mean_cost",
    "median_cost",
    "data_year",
]

FIELDS = {
    "pfi_number": ("pfi", "permanent_facility_id", "facility_id"),
    "facility_name": ("facility_name", "hospital_name"),
    "apr_drg_code": ("apr_drg_code",),
    "apr_drg_description": ("apr_drg_description",),
    "apr_severity_of_illness_code": ("apr_severity_of_illness_code",),
    "medical_surgical_code": ("apr_medical_surgical_code",),
    "discharges": ("discharges",),
    "mean_charge": ("mean_charge",),
    "median_charge": ("median_charge",),
    "mean_cost": ("mean_cost",),
    "median_cost": ("median_cost",),
    "data_year": ("year", "discharge_year"),
}

INT_COLUMNS = {
    "pfi_number", "apr_drg_code", "apr_severity_of_illness_code",
    "discharges", "data_year",
}
NUM_COLUMNS = {"mean_charge", "median_charge", "mean_cost", "median_cost"}

# The file spans APR-DRG versions, so a bare code number is ambiguous across
# years. Requiring the description to say "joint replacement" pins it to the
# right procedure regardless of vintage -- the same guard the MRF parser uses.
_JOINT = re.compile(r"(hip|knee)\s+joint\s+replacement", re.I)


def _missing(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, float) and value != value)


def _as_int(value: Any) -> int | None:
    if _missing(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _as_num(value: Any) -> float | None:
    if _missing(value):
        return None
    text = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _normalize(record: dict) -> dict | None:
    out: dict[str, Any] = {}
    for target, candidates in FIELDS.items():
        value = None
        for name in candidates:
            if name in record and not _missing(record[name]):
                value = record[name]
                break
        if target in INT_COLUMNS:
            out[target] = _as_int(value)
        elif target in NUM_COLUMNS:
            out[target] = _as_num(value)
        else:
            out[target] = str(value).strip() if value is not None else None

    if not _JOINT.search(out.get("apr_drg_description") or ""):
        return None
    if out.get("pfi_number") is None:
        return None
    return out


def fetch_cost_data(truncate_first: bool = True) -> int:
    """Load facility-level joint-replacement cost rows into stg_sparcs_cost."""
    dataset = config.SPARCS_COST_DATASET_ID
    codes = ", ".join(f"'{c}'" for c in config.SPARCS_COST_DRGS)

    client = Socrata(config.SOCRATA_DOMAIN, config.SOCRATA_APP_TOKEN, timeout=120)
    if truncate_first:
        db.truncate("stg_sparcs_cost")

    total = 0
    try:
        # Filter server-side on the code, then confirm client-side on the
        # description -- the code alone means different things by vintage.
        where = f"apr_drg_code in ({codes})"
        offset = 0
        batch: list[dict] = []
        while True:
            page = client.get(
                dataset, where=where, limit=config.SOCRATA_PAGE_SIZE,
                offset=offset, order=":id",
            )
            if not page:
                break
            for record in page:
                norm = _normalize(record)
                if norm:
                    batch.append(norm)
            if batch:
                total += db.copy_records("stg_sparcs_cost", TARGET_COLUMNS, batch)
                batch.clear()
            log.info("  %s rows kept after %s scanned...", total, offset + len(page))
            if len(page) < config.SOCRATA_PAGE_SIZE:
                break
            offset += config.SOCRATA_PAGE_SIZE
    finally:
        client.close()

    if total == 0:
        log.error(
            "No cost rows matched. Check that dataset %s still exposes "
            "apr_drg_code and apr_drg_description.", dataset,
        )
    else:
        log.info("Inserted %s SPARCS cost rows into stg_sparcs_cost.", total)
    return total

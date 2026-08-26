"""SPARCS clinical data ingestion from the Health Data NY Socrata API."""

from __future__ import annotations

import logging
from typing import Any, Iterator

from sodapy import Socrata

import config
from etl import db

log = logging.getLogger(__name__)

TARGET_COLUMNS = [
    "permanent_facility_id",
    "operating_certificate_number",
    "facility_name",
    "hospital_county",
    "hospital_service_area",
    "zip_code_3_digit",
    "age_group",
    "type_of_admission",
    "apr_drg_code",
    "apr_drg_description",
    "apr_severity_of_illness_code",
    "apr_risk_of_mortality",
    "length_of_stay_raw",
    "patient_disposition",
    "discharge_year",
    "source_dataset_id",
]

# SPARCS field names drift between release years -- zip_code_3_digits gained and
# lost its trailing 's', the PFI column has been published under three names.
# Selecting a fixed list of columns, as the original script did, raises a bare
# KeyError on any year where one name differs. Each target maps to candidates in
# priority order; a missing column yields None rather than an exception.
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "permanent_facility_id": (
        "permanent_facility_id",
        "facility_id",
        "permanent_facility_identifier",
    ),
    "operating_certificate_number": ("operating_certificate_number",),
    "facility_name": ("facility_name", "hospital_name"),
    "hospital_county": ("hospital_county", "county"),
    # 2022 calls this hospital_service_area; 2024 calls it health_service_area.
    "hospital_service_area": ("hospital_service_area", "health_service_area"),
    # 2022 publishes zip_code_3_digits; 2024 publishes zip_code.
    "zip_code_3_digit": ("zip_code_3_digits", "zip_code_3_digit", "zip_code"),
    "age_group": ("age_group",),
    "type_of_admission": ("type_of_admission",),
    "apr_drg_code": ("apr_drg_code",),
    "apr_drg_description": ("apr_drg_description",),
    "apr_severity_of_illness_code": ("apr_severity_of_illness_code",),
    "apr_risk_of_mortality": ("apr_risk_of_mortality",),
    "length_of_stay_raw": ("length_of_stay",),
    "patient_disposition": ("patient_disposition",),
    "discharge_year": ("discharge_year",),
}

INT_COLUMNS = {
    "permanent_facility_id",
    "apr_drg_code",
    "apr_severity_of_illness_code",
    "discharge_year",
}


def _is_missing(value: Any) -> bool:
    """None, empty string, or a float NaN (which is what pandas yields)."""
    if value is None or value == "":
        return True
    return isinstance(value, float) and value != value  # NaN is not equal to itself


def _as_int(value: Any) -> int | None:
    """Socrata returns every value as a string, including numerics."""
    if _is_missing(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _normalize(record: dict, dataset_id: str) -> dict:
    out: dict[str, Any] = {}
    for target, candidates in FIELD_CANDIDATES.items():
        value = None
        for name in candidates:
            if name in record and not _is_missing(record[name]):
                value = record[name]
                break
        if target in INT_COLUMNS:
            out[target] = _as_int(value)
        else:
            out[target] = str(value).strip() if value is not None else None
    out["source_dataset_id"] = dataset_id
    return out


def _where_clause(client: Socrata, dataset_id: str) -> str:
    """Pick a WHERE clause that matches the dataset's actual column type.

    apr_drg_code is text in SPARCS (values are zero-padded, e.g. '001'), but the
    numeric form is tried as a fallback in case a future release changes it.

    A candidate clause is accepted only if it actually RETURNS ROWS. Accepting a
    clause merely because it did not raise is how the wrong DRG codes went
    unnoticed: `apr_drg_code in ('301','302')` is perfectly valid SoQL against a
    text column and returns an empty set, so a probe that only checks for an
    exception reports success on a filter that matches nothing.
    """
    quoted = ", ".join(f"'{c}'" for c in config.APR_DRG_CODES)
    unquoted = ", ".join(config.APR_DRG_CODES)

    for clause in (f"apr_drg_code in ({quoted})", f"apr_drg_code in ({unquoted})"):
        try:
            probe = client.get(dataset_id, where=clause, limit=1)
        except Exception as exc:  # noqa: BLE001 - probing for the working form
            log.debug("filter %r rejected: %s", clause, exc)
            continue
        if probe:
            log.info("using SoQL filter: %s", clause)
            return clause
        log.debug("filter %r valid but matched nothing", clause)

    raise RuntimeError(
        f"No apr_drg_code filter matched any rows in dataset {dataset_id} for "
        f"codes {config.APR_DRG_CODES}. Confirm the codes exist:\n"
        f"  https://{config.SOCRATA_DOMAIN}/resource/{dataset_id}.json"
        f"?$select=apr_drg_code,apr_drg_description&$group=apr_drg_code,apr_drg_description"
    )


def _paged_records(client: Socrata, dataset_id: str, where: str) -> Iterator[dict]:
    """Page through the API.

    The original single call with limit=150000 silently truncates as soon as the
    cohort exceeds that number, with no way to tell a complete pull from a
    clipped one.
    """
    offset = 0
    fetched = 0
    while True:
        page = client.get(
            dataset_id,
            where=where,
            limit=config.SOCRATA_PAGE_SIZE,
            offset=offset,
            order=":id",  # stable ordering; without it paging can repeat rows
        )
        if not page:
            break

        for record in page:
            yield record
            fetched += 1
            if config.SOCRATA_MAX_RECORDS and fetched >= config.SOCRATA_MAX_RECORDS:
                log.warning("stopping at SOCRATA_MAX_RECORDS=%s", config.SOCRATA_MAX_RECORDS)
                return

        log.info("  fetched %s records...", fetched)
        offset += config.SOCRATA_PAGE_SIZE

        if len(page) < config.SOCRATA_PAGE_SIZE:
            break


def _fetch_one_dataset(client: Socrata, year: str, dataset_id: str) -> int:
    log.info("Fetching SPARCS %s (dataset %s)...", year, dataset_id)
    where = _where_clause(client, dataset_id)

    batch: list[dict] = []
    total = 0
    for record in _paged_records(client, dataset_id, where):
        batch.append(_normalize(record, dataset_id))
        if len(batch) >= 10000:
            total += db.copy_records("stg_sparcs", TARGET_COLUMNS, batch)
            batch.clear()

    if batch:
        total += db.copy_records("stg_sparcs", TARGET_COLUMNS, batch)

    if total == 0:
        log.error(
            "  no records returned for %s. Verify that dataset %s exposes an "
            "apr_drg_code column containing 301/302.",
            year, dataset_id,
        )
    else:
        log.info("  inserted %s records for %s.", total, year)
    return total


def fetch_sparcs(truncate_first: bool = True) -> int:
    """Load every configured SPARCS release year into stg_sparcs."""
    datasets = config.sparcs_datasets()

    if not config.SOCRATA_APP_TOKEN:
        log.warning(
            "No SOCRATA_APP_TOKEN set. Anonymous requests are throttled hard and "
            "a full pull may take a long time or start returning 403s. Register a "
            "free token at https://%s/profile/edit/developer_settings",
            config.SOCRATA_DOMAIN,
        )

    if truncate_first:
        # Truncated once, before the loop -- truncating per dataset would leave
        # only the last year loaded.
        db.truncate("stg_sparcs")

    client = Socrata(config.SOCRATA_DOMAIN, config.SOCRATA_APP_TOKEN, timeout=120)
    grand_total = 0
    failures: list[str] = []
    try:
        for year, dataset_id in datasets:
            try:
                grand_total += _fetch_one_dataset(client, year, dataset_id)
            except Exception as exc:  # noqa: BLE001 - one bad year must not lose the rest
                log.error("  failed to load SPARCS %s (%s): %s", year, dataset_id, exc)
                failures.append(year)
    finally:
        client.close()

    log.info(
        "Inserted %s SPARCS records across %s year(s) into stg_sparcs.",
        grand_total, len(datasets) - len(failures),
    )
    if failures:
        log.warning("Years that failed to load: %s", ", ".join(failures))

    return grand_total


def load_sparcs_csv(path: str, truncate_first: bool = True) -> int:
    """Offline fallback: load a SPARCS Public Use File downloaded as CSV.

    The API is the primary path, but Health Data NY throttles anonymous callers
    and occasionally takes datasets offline during a refresh. Roadmap Step 4
    describes downloading the PUF directly, so support that too.
    """
    import pandas as pd

    log.info("Loading SPARCS from %s ...", path)
    if truncate_first:
        db.truncate("stg_sparcs")

    total = 0
    for chunk in pd.read_csv(path, dtype=str, chunksize=50000, low_memory=False):
        chunk.columns = [
            c.strip().lower().replace(" ", "_").replace("/", "_")
            for c in chunk.columns
        ]
        records = [
            _normalize(rec, f"csv:{path}")
            for rec in chunk.to_dict(orient="records")
        ]
        total += db.copy_records("stg_sparcs", TARGET_COLUMNS, records)
        log.info("  loaded %s rows...", total)

    log.info("Inserted %s SPARCS records into stg_sparcs.", total)
    return total

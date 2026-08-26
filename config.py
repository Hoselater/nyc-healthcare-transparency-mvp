"""Central configuration, loaded from the environment.

Nothing secret is hard-coded here. Copy .env.example to .env and fill it in;
.env is gitignored, which is what the project README asks for.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

SQL_DIR = PROJECT_ROOT / "sql"
DATA_DIR = PROJECT_ROOT / "data"
EXPORT_DIR = PROJECT_ROOT / "exports"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def database_url() -> str:
    """Build the SQLAlchemy URL, preferring an explicit DATABASE_URL."""
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit

    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "nyc_healthcare_mvp")

    # Empty counts as missing: .env.example ships DB_PASSWORD= with no value, so
    # an `is None` check would sail past it and fail later with a confusing
    # authentication error instead of a clear one here.
    if not password:
        raise RuntimeError(
            "No database password found. Copy .env.example to .env and set "
            "DB_PASSWORD (or set DATABASE_URL directly)."
        )

    # quote_plus so passwords containing @ : / # survive URL parsing.
    return (
        f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}"
    )


# ---------------------------------------------------------------------------
# SPARCS (Health Data NY / Socrata)
# ---------------------------------------------------------------------------
SOCRATA_DOMAIN = "health.data.ny.gov"

# Each SPARCS release year is a distinct Socrata dataset ("4x4" id). The
# original script hard-coded 5dtw-tffi with no indication of which year that
# was, which quietly pins the whole project to 2022.
#
# Only the two ids below are verified from the project's own source citations.
# To add another year, find it at https://health.data.ny.gov (search "Hospital
# Inpatient Discharges SPARCS De-Identified"), take the 4x4 from the URL, and
# add it here -- or just set SPARCS_DATASET_ID in .env to override.
SPARCS_DATASETS = {
    "2022": "5dtw-tffi",
    "2024": "sf4k-39ay",
}

SPARCS_YEARS = [
    y.strip() for y in os.getenv("SPARCS_YEARS", "2022,2024").split(",") if y.strip()
]

# Explicit override: pins the load to one dataset regardless of SPARCS_YEARS.
SPARCS_DATASET_ID = os.getenv("SPARCS_DATASET_ID") or None

SOCRATA_APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN") or None


def sparcs_datasets() -> list[tuple[str, str]]:
    """Resolve the configured years to (year, dataset_id) pairs.

    Stacking multiple release years raises per-facility volume, which stabilises
    the O/E ratios for lower-volume hospitals. The severity benchmarks are
    stratified by discharge year downstream, so pooling years does not smear
    year-over-year drift in average length of stay into the comparison.
    """
    if SPARCS_DATASET_ID:
        return [("explicit", SPARCS_DATASET_ID)]

    resolved: list[tuple[str, str]] = []
    unknown: list[str] = []
    for year in SPARCS_YEARS:
        dataset = SPARCS_DATASETS.get(year)
        if dataset:
            resolved.append((year, dataset))
        else:
            unknown.append(year)

    if unknown:
        raise RuntimeError(
            f"No Socrata dataset id known for SPARCS year(s): {', '.join(unknown)}. "
            f"Known years: {', '.join(sorted(SPARCS_DATASETS))}. Add the 4x4 to "
            f"config.SPARCS_DATASETS, or set SPARCS_DATASET_ID to override."
        )
    if not resolved:
        raise RuntimeError("SPARCS_YEARS is empty.")
    return resolved

# APR-DRG codes for ELECTIVE primary joint replacement.
#
# The project's planning documents specify 301 (hip) and 302 (knee). Those codes
# do not exist in the SPARCS data: verified against both the 2022 and 2024
# releases, 301 and 302 return zero rows, and 303 is a lumbar fusion procedure.
# The APR-DRG v38 classification actually used by SPARCS is:
#
#     323  NON-ELECTIVE OR COMPLEX HIP JOINT REPLACEMENT
#     324  ELECTIVE HIP JOINT REPLACEMENT
#     325  NON-ELECTIVE OR COMPLEX KNEE JOINT REPLACEMENT
#     326  ELECTIVE KNEE JOINT REPLACEMENT
#
# 324 and 326 are the elective cohort the MVP scopes itself to, and the codes
# carry the elective distinction directly. 323/325 are the non-elective and
# complex counterparts, deliberately excluded.
APR_DRG_HIP_ELECTIVE = "324"
APR_DRG_KNEE_ELECTIVE = "326"
APR_DRG_CODES = (APR_DRG_HIP_ELECTIVE, APR_DRG_KNEE_ELECTIVE)
APR_DRG_NON_ELECTIVE = ("323", "325")

TARGET_MS_DRG = os.getenv("TARGET_MS_DRG", "470")

# Some hospitals publish inpatient pricing under APR-DRG rather than MS-DRG.
# NYU Langone's MRF, for instance, codes every DRG line as 'APR470-1' with a
# declared type of 'DRG' -- that is APR-DRG 470, an entirely different
# classification from MS-DRG 470, and treating them as equivalent would import
# an unrelated procedure's price.
#
# Matching APR-DRG 324/326 instead recovers those facilities correctly, and maps
# to exactly the same clinical cohort the SPARCS side already uses.
TARGET_APR_DRGS = tuple(
    c.strip() for c in os.getenv("TARGET_APR_DRGS", "324,326").split(",") if c.strip()
)

# Legacy APR-DRG numbering. Hospital chargemasters are not all on the same
# APR-DRG version as SPARCS. NYU Langone's file codes hip and knee replacement
# as APR301 and APR302 -- verified from their own description column:
#
#     APR301-1  HIP JOINT REPLACEMENT      negotiated $22,288 - $64,634
#     APR302-1  KNEE JOINT REPLACEMENT     negotiated $21,547 - $62,486
#
# That is the numbering the project's original planning documents cited, and it
# is correct for the older APR-DRG version -- SPARCS simply moved to v38, where
# the same procedures became 323-326. Both vintages are therefore accepted on
# the PRICING side.
#
# Because a bare number is weak evidence across versions, legacy codes are only
# accepted when the row's own description confirms the procedure. See
# LEGACY_DESCRIPTION_PATTERN below.
TARGET_APR_DRGS_LEGACY = tuple(
    c.strip()
    for c in os.getenv("TARGET_APR_DRGS_LEGACY", "301,302").split(",")
    if c.strip()
)

# A legacy-coded row must describe a joint replacement to be trusted.
LEGACY_DESCRIPTION_PATTERN = os.getenv(
    "LEGACY_DESCRIPTION_PATTERN",
    r"(joint\s+replacement|arthroplasty|(hip|knee)\s+replacement)",
)

# SPARCS Cost Transparency: facility-level median charge and median cost per
# APR-DRG and severity tier, back to 2009, keyed on PFI.
#
# Strategically this is the most valuable free source in the project. It needs
# no crawling, no entity resolution (the PFI is the join key the pipeline
# already uses), and it covers EVERY Article 28 facility -- where CMS price-file
# crawling reaches maybe a quarter of them and is blocked outright at several.
#
# It measures something different from a negotiated rate, and that distinction
# must never be blurred:
#   * median_charge -- the hospital's list price. Almost nobody pays this.
#   * median_cost   -- the hospital's own reported cost of delivering care,
#                      from the Institutional Cost Report.
#   * CMS MRF       -- what a specific payer actually negotiated.
# The MRF figure is the one a patient's liability derives from, so it stays the
# primary basis; these fill the gaps and are labelled as such in the output.
SPARCS_COST_DATASET_ID = os.getenv("SPARCS_COST_DATASET_ID", "7dtz-qxmr")

# The cost file spans years using two APR-DRG vintages, so both are accepted and
# the description is required to confirm the procedure.
SPARCS_COST_DRGS = tuple(
    c.strip()
    for c in os.getenv("SPARCS_COST_DRGS", "301,302,324,326").split(",")
    if c.strip()
)

SOCRATA_PAGE_SIZE = int(os.getenv("SOCRATA_PAGE_SIZE", "50000"))
SOCRATA_MAX_RECORDS = int(os.getenv("SOCRATA_MAX_RECORDS", "0"))  # 0 = no cap


# ---------------------------------------------------------------------------
# CMS MRF crawling
# ---------------------------------------------------------------------------
TARGET_HOSPITALS_CSV = DATA_DIR / "target_hospitals.csv"

# A descriptive agent is the polite default, but several hospital CDNs answer
# non-browser agents with 403 (Mount Sinai's MRF host does). The crawler falls
# back to the browser agent on a 403 rather than losing the facility.
HTTP_HEADERS = {
    "User-Agent": os.getenv(
        "HTTP_USER_AGENT",
        "nyc-healthcare-transparency-mvp/1.0 (portfolio research project)",
    ),
    "Accept": "*/*",
}

HTTP_HEADERS_FALLBACK = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
HTTP_TIMEOUT = (15, 180)          # (connect, read) seconds

# Courtesy pause between requests to hospital servers. The cms-hpt.txt protocol
# exists to invite automated collection, but that is not licence to hammer a
# hospital's web server from a public repository. One second between hosts costs
# under a minute across the whole crawl.
CRAWL_DELAY_SECONDS = float(os.getenv("CRAWL_DELAY_SECONDS", "1.0"))
MRF_BATCH_ROWS = int(os.getenv("MRF_BATCH_ROWS", "5000"))
MRF_MAX_ROWS_SCANNED = int(os.getenv("MRF_MAX_ROWS_SCANNED", "0"))  # 0 = unlimited


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------
# Below this rapidfuzz score a suggested pairing is not written to the
# crosswalk at all. Between this and FUZZY_AUTO_ACCEPT it is written with
# reviewed = FALSE and must be confirmed by a human.
FUZZY_MIN_SCORE = float(os.getenv("FUZZY_MIN_SCORE", "60"))
FUZZY_AUTO_ACCEPT = float(os.getenv("FUZZY_AUTO_ACCEPT", "95"))

"""Entity resolution between SPARCS and CMS facility names.

State filings and federal pricing files rarely spell a hospital the same way
("MOUNT SINAI HOSPITAL" vs "The Mount Sinai Hospital"), so a direct string join
loses most of the market. This builds a reviewable crosswalk instead.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from rapidfuzz import fuzz, process

import config
from etl import db

log = logging.getLogger(__name__)

REVIEW_CSV = config.DATA_DIR / "facility_crosswalk_review.csv"

# Corporate noise that carries no identifying signal but wrecks similarity
# scores. Removed before comparison, never from the stored names.
_NOISE = re.compile(
    r"\b(the|inc|incorporated|llc|corp|corporation|of|at|and|"
    r"hospital|hospitals|medical|center|centre|health|healthcare|"
    r"system|systems|campus|division|dba)\b",
    re.I,
)
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACE = re.compile(r"\s+")


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    text = name.lower()
    text = text.replace("&", " and ").replace("-", " ").replace("'", "")
    text = _PUNCT.sub(" ", text)
    text = _NOISE.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    for src, dst in _ALIASES.items():
        if src in text:
            text = text.replace(src, dst)
    return _SPACE.sub(" ", text).strip()


# Abbreviations and rebrandings no string-similarity metric can bridge, because
# the two forms share almost no characters. Each was confirmed by inspection.
_ALIASES = {
    "sbh": "st barnabas",
    "rumc": "richmond university",
    "nyp": "newyork presbyterian",
    "new york presbyterian": "newyork presbyterian",
    "nyu langone hospitals": "nyu langone tisch",
    "mount sinai mount sinai queens": "mount sinai queens",
    "hospital for special surgery": "special surgery",
}

_STOPWORDS = {"new", "york", "the", "and", "for", "of"}


def significant_tokens(normalized: str) -> set[str]:
    """Tokens carrying identifying signal, used as a match precondition.

    Pure similarity scores are noisy across a pool of 60+ hospital names:
    'nyu langone orthopedic' scores 42 against 'Peconic Bay Medical Center',
    which is meaningless. Requiring at least one substantial shared token kills
    that class of coincidence outright, which in turn makes it safe to lower the
    similarity threshold far enough to catch the real near-misses -- NYP
    Columbia against 'NewYork-Presbyterian Columbia University Irving Medical
    Center' only scores 67.
    """
    return {t for t in normalized.split() if len(t) >= 4 and t not in _STOPWORDS}


def _fetch_sparcs_facilities() -> list[dict]:
    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(
                """
                SELECT pfi_number, facility_name, patient_volume
                FROM facility_clinical_metrics
                ORDER BY patient_volume DESC
                """
            )
            return [
                {"pfi": r[0], "name": r[1], "volume": r[2]} for r in cur.fetchall()
            ]
    finally:
        raw.close()


def _fetch_cms_facilities() -> list[str]:
    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT facility_name
                FROM stg_cms_mrf
                WHERE facility_name IS NOT NULL AND btrim(facility_name) <> ''
                """
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        raw.close()


def _existing_manual() -> tuple[set[int], set[str]]:
    """Rows a human has signed off on: never overwritten, and their CMS names
    are already spoken for.

    Returning the claimed CMS names matters because cms_facility_name carries a
    UNIQUE index. Without pre-seeding them, the matcher can hand an already-taken
    CMS name to a different SPARCS facility and the upsert dies on a constraint
    violation partway through.
    """
    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(
                """
                SELECT pfi_number, cms_facility_name
                FROM facility_crosswalk
                WHERE reviewed = TRUE AND pfi_number IS NOT NULL
                """
            )
            rows = cur.fetchall()
            return ({r[0] for r in rows}, {r[1] for r in rows if r[1]})
    finally:
        raw.close()


def build(write_review_csv: bool = True) -> int:
    """Propose SPARCS -> CMS pairings and upsert them into facility_crosswalk."""
    sparcs = _fetch_sparcs_facilities()
    cms = _fetch_cms_facilities()
    protected, already_claimed = _existing_manual()

    if not sparcs:
        raise RuntimeError(
            "facility_clinical_metrics is empty. Run the SPARCS load and "
            "02_transformations.sql before building the crosswalk."
        )

    log.info("Matching %s SPARCS facilities against %s CMS facilities.",
             len(sparcs), len(cms))

    cms_by_norm: dict[str, str] = {}
    for name in cms:
        cms_by_norm.setdefault(normalize_name(name), name)

    choices = list(cms_by_norm.keys())
    claimed: set[str] = set(already_claimed)  # enforces the one-to-one constraint
    proposals: list[dict] = []

    # Highest-volume facilities pick first, so when two SPARCS names compete for
    # one CMS name the busier hospital wins the pairing.
    for facility in sparcs:
        if facility["pfi"] in protected:
            log.debug("keeping reviewed mapping for %s", facility["name"])
            continue

        norm = normalize_name(facility["name"])
        tokens = significant_tokens(norm)
        match_name: str | None = None
        method: str | None = None
        score: float | None = None

        if norm and norm in cms_by_norm and cms_by_norm[norm] not in claimed:
            match_name = cms_by_norm[norm]
            method, score = "exact", 100.0
        elif norm and choices:
            # Only names sharing an identifying token are even eligible.
            available = [
                c
                for c in choices
                if cms_by_norm[c] not in claimed and tokens & significant_tokens(c)
            ]
            if available:
                best = process.extractOne(
                    norm, available,
                    # token_set_ratio, not token_sort_ratio: hospital names differ
                    # by extra words far more often than by word order, and
                    # token_set forgives the extra words.
                    scorer=fuzz.token_set_ratio,
                    score_cutoff=config.FUZZY_MIN_SCORE,
                )
                if best:
                    match_name = cms_by_norm[best[0]]
                    method, score = "fuzzy", float(best[1])

        if match_name:
            claimed.add(match_name)

        proposals.append(
            {
                "pfi_number": facility["pfi"],
                "sparcs_facility_name": facility["name"],
                "cms_facility_name": match_name,
                "match_method": method,
                "match_score": score,
                # Exact matches are trusted; fuzzy ones need eyes on them unless
                # they are near-identical.
                "reviewed": bool(
                    method == "exact"
                    or (score is not None and score >= config.FUZZY_AUTO_ACCEPT)
                ),
                "volume": facility["volume"],
            }
        )

    _upsert(proposals)

    if write_review_csv:
        _write_review_csv(proposals)

    matched = sum(1 for p in proposals if p["cms_facility_name"])
    needs_review = sum(
        1 for p in proposals if p["cms_facility_name"] and not p["reviewed"]
    )
    log.info(
        "Crosswalk: %s/%s facilities matched, %s awaiting review.",
        matched, len(proposals), needs_review,
    )
    if needs_review:
        log.info("Review and correct: %s", REVIEW_CSV)

    return matched


def _upsert(proposals: list[dict]) -> None:
    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            for p in proposals:
                cur.execute(
                    """
                    INSERT INTO facility_crosswalk
                        (pfi_number, sparcs_facility_name, cms_facility_name,
                         match_method, match_score, reviewed, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (pfi_number)
                    DO UPDATE SET
                        sparcs_facility_name = EXCLUDED.sparcs_facility_name,
                        cms_facility_name = EXCLUDED.cms_facility_name,
                        match_method      = EXCLUDED.match_method,
                        match_score       = EXCLUDED.match_score,
                        reviewed          = EXCLUDED.reviewed,
                        updated_at        = now()
                    WHERE facility_crosswalk.reviewed = FALSE
                    """,
                    (
                        p["pfi_number"],
                        p["sparcs_facility_name"],
                        p["cms_facility_name"],
                        p["match_method"],
                        p["match_score"],
                        p["reviewed"],
                    ),
                )
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _write_review_csv(proposals: list[dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fields = [
        "sparcs_facility_name",
        "cms_facility_name",
        "pfi_number",
        "match_method",
        "match_score",
        "reviewed",
        "volume",
    ]
    with open(REVIEW_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for p in sorted(proposals, key=lambda x: (x["reviewed"], -(x["volume"] or 0))):
            writer.writerow({k: p.get(k) for k in fields})


def import_reviewed(path: str | Path = REVIEW_CSV) -> int:
    """Load a hand-corrected crosswalk CSV back into the database.

    Set reviewed to true on the rows you have checked; those become immutable
    against later automated rebuilds.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    # Fail before touching the database, with a message that names the problem.
    # cms_facility_name is UNIQUE; a hand-edited file that points two SPARCS
    # facilities at one CMS name would otherwise abort mid-import.
    seen: dict[str, str] = {}
    for row in rows:
        cms_name = (row.get("cms_facility_name") or "").strip().lower()
        if not cms_name:
            continue
        if cms_name in seen:
            raise ValueError(
                f"{path.name}: '{row.get('cms_facility_name')}' is mapped to both "
                f"'{seen[cms_name]}' and '{row.get('sparcs_facility_name')}'. "
                f"Each CMS facility may map to exactly one SPARCS facility."
            )
        seen[cms_name] = row.get("sparcs_facility_name", "")

    missing_pfi = [
        r.get("sparcs_facility_name", "?")
        for r in rows
        if not (r.get("pfi_number") or "").strip().isdigit()
    ]
    if missing_pfi:
        raise ValueError(
            f"{path.name}: {len(missing_pfi)} row(s) have no numeric pfi_number, "
            f"which is the crosswalk key. First few: {missing_pfi[:5]}"
        )

    raw = db.get_engine().raw_connection()
    count = 0
    try:
        with raw.cursor() as cur:
            for row in rows:
                sparcs_name = (row.get("sparcs_facility_name") or "").strip()
                if not sparcs_name:
                    continue
                cms_name = (row.get("cms_facility_name") or "").strip() or None
                reviewed = str(row.get("reviewed", "")).strip().lower() in {
                    "true", "t", "yes", "y", "1"
                }
                pfi = (row.get("pfi_number") or "").strip()
                cur.execute(
                    """
                    INSERT INTO facility_crosswalk
                        (pfi_number, sparcs_facility_name, cms_facility_name,
                         match_method, reviewed, updated_at)
                    VALUES (%s, %s, %s, 'manual', %s, now())
                    ON CONFLICT (pfi_number)
                    DO UPDATE SET
                        sparcs_facility_name = EXCLUDED.sparcs_facility_name,
                        cms_facility_name = EXCLUDED.cms_facility_name,
                        match_method      = 'manual',
                        reviewed          = EXCLUDED.reviewed,
                        updated_at        = now()
                    """,
                    (int(pfi), sparcs_name, cms_name, reviewed),
                )
                count += 1
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    log.info("Imported %s crosswalk rows from %s", count, path)
    return count


# ---------------------------------------------------------------------------
# CCN crosswalk: SPARCS facilities <-> CMS Care Compare
# ---------------------------------------------------------------------------
def build_ccn(min_score: float = 82.0) -> int:
    """Match scored NYC facilities to their CMS Certification Number.

    Easier than the MRF crosswalk: both sides use official regulatory naming,
    so exact and near-exact matches dominate. A higher threshold is used here
    precisely because the names are cleaner -- a loose match would be a sign of
    something wrong, not of a name variant.
    """
    sparcs = _fetch_sparcs_facilities()
    if not sparcs:
        raise RuntimeError("No scored facilities. Run transform first.")

    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cms_certification_number, facility_name, citytown
                FROM stg_cms_quality
                WHERE cms_certification_number <> ''
                """
            )
            cms = cur.fetchall()
    finally:
        raw.close()

    if not cms:
        raise RuntimeError("stg_cms_quality is empty. Run `cost`/`quality` first.")

    by_norm: dict[str, tuple[str, str]] = {}
    for ccn, name, city in cms:
        by_norm.setdefault(normalize_name(name), (ccn, name))

    choices = list(by_norm)
    claimed: set[str] = set()
    matched = 0
    rows: list[tuple] = []

    for facility in sparcs:
        norm = normalize_name(facility["name"])
        tokens = significant_tokens(norm)
        if not norm:
            continue

        pick = None
        score = None
        if norm in by_norm and by_norm[norm][0] not in claimed:
            pick, score = by_norm[norm], 100.0
        else:
            available = [
                c for c in choices
                if by_norm[c][0] not in claimed and tokens & significant_tokens(c)
            ]
            if available:
                best = process.extractOne(
                    norm, available, scorer=fuzz.token_set_ratio, score_cutoff=min_score
                )
                if best:
                    pick, score = by_norm[best[0]], float(best[1])

        if not pick:
            continue
        ccn, cms_name = pick
        claimed.add(ccn)
        matched += 1
        rows.append((facility["pfi"], ccn, facility["name"], cms_name,
                     "exact" if score == 100.0 else "fuzzy", score))

    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO facility_ccn_crosswalk
                        (pfi_number, cms_certification_number, sparcs_facility_name,
                         cms_facility_name, match_method, match_score, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (pfi_number) DO UPDATE SET
                        cms_certification_number = EXCLUDED.cms_certification_number,
                        cms_facility_name        = EXCLUDED.cms_facility_name,
                        match_method             = EXCLUDED.match_method,
                        match_score              = EXCLUDED.match_score,
                        updated_at               = now()
                    WHERE facility_ccn_crosswalk.reviewed = FALSE
                    """,
                    r,
                )
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    log.info("CCN crosswalk: %s/%s facilities matched to CMS.", matched, len(sparcs))
    return matched

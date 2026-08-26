"""Export the scored market to CSV for Tableau."""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

import config
from etl import db

log = logging.getLogger(__name__)


def export_scores(path: str | Path | None = None, include_incomplete: bool = False) -> Path:
    """Write the scored facility table to a CSV Tableau Public can open.

    The README's `COPY (...) TO '/path/to/desktop/file.csv'` runs on the SERVER,
    not the client: it needs superuser (or pg_write_server_files) and writes to
    the database host's filesystem, which is the wrong machine as soon as the
    database moves to Supabase or Neon. Pulling the rows client-side works
    everywhere and needs no elevated grant.
    """
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(path) if path else config.EXPORT_DIR / (
        f"nyc_ortho_scores_{date.today():%Y%m%d}.csv"
    )

    view = "vw_facility_scores" if include_incomplete else "vw_tableau_export"
    order = " ORDER BY value_index DESC NULLS LAST" if include_incomplete else ""

    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(f"SELECT * FROM {view}{order}")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        raw.close()

    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)

    log.info("Exported %s facilities to %s", len(rows), target)
    if not rows:
        log.warning(
            "Export is empty. Check 05_qa_checks.sql -- most likely the "
            "crosswalk has no reviewed mappings, or no MRF prices were ingested."
        )
    return target


# The export paths committed to git and read by the public web app.
PUBLIC_SNAPSHOT = config.DATA_DIR / "nyc_ortho_scores_public.csv"
PROCEDURE_SNAPSHOT = config.DATA_DIR / "nyc_ortho_scores_by_procedure.csv"


def _dump(view: str, target: Path, order: str = "") -> int:
    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(f"SELECT * FROM {view}{order}")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        raw.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return len(rows)


def publish_snapshot() -> Path:
    """Write the snapshot the deployed Streamlit app falls back to.

    Kept small and committed deliberately: the scored table is a few dozen rows,
    so shipping it in the repo means the public demo works even when no cloud
    database is attached, or when a free-tier database has been suspended for
    inactivity.
    """
    target = export_scores(PUBLIC_SNAPSHOT)

    # Per-procedure scores, so the app can answer "how good is this hospital at
    # MY operation" rather than only at joint replacement in aggregate.
    try:
        count = _dump(
            "vw_procedure_scores", PROCEDURE_SNAPSHOT,
            " ORDER BY procedure, value_index DESC",
        )
        log.info("Published %s per-procedure rows to %s", count, PROCEDURE_SNAPSHOT.name)
    except Exception as exc:  # noqa: BLE001 - the combined snapshot is the critical one
        log.warning("Per-procedure snapshot skipped: %s", exc)

    log.info(
        "Published snapshot. Commit it so the deployed app picks it up:\n"
        "    git add data/ && git commit -m 'Update scored snapshot'",
    )
    return target

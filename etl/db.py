"""Database plumbing: engine, SQL script execution, and fast bulk loading."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import config

log = logging.getLogger(__name__)

_engine: Engine | None = None


def get_engine() -> Engine:
    """Process-wide engine. pool_pre_ping survives an idle Postgres restart."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.database_url(),
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def run_sql_file(path: str | Path) -> None:
    """Execute a .sql script as a single transaction.

    Deliberately does NOT split on semicolons: the schema defines dollar-quoted
    function bodies ($fn$ ... $fn$) that contain semicolons, and naive splitting
    tears them in half. Handing the whole file to one cursor.execute() also
    sidesteps SQLAlchemy's text() treating `::numeric` casts as bind parameters.

    psql meta-commands (\\echo, \\i, \\set) are not understood by the server, so
    scripts containing them -- 05_qa_checks.sql and 99_smoke_test.sql -- must be
    run through psql instead.
    """
    path = Path(path)
    sql = path.read_text(encoding="utf-8")

    if any(line.lstrip().startswith("\\") for line in sql.splitlines()):
        raise ValueError(
            f"{path.name} contains psql meta-commands and must be run with psql, "
            f"not through this runner."
        )

    raw = get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql)
        raw.commit()
        log.info("ran %s", path.name)
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def copy_records(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Mapping[str, object]],
) -> int:
    """Bulk-load dict rows with COPY ... FROM STDIN.

    Replaces DataFrame.to_sql(method="multi"), which builds one giant INSERT and
    blows past psycopg2's 65,535-parameter ceiling: 150k SPARCS rows x 13 columns
    is roughly two million placeholders.

    An empty CSV field is COPY's default NULL representation, so None values
    round-trip correctly. Genuine empty strings therefore also arrive as NULL,
    which is the desired behaviour for every column here.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    count = 0
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
        count += 1

    if count == 0:
        return 0

    buf.seek(0)
    collist = ", ".join(f'"{c}"' for c in columns)
    sql = f'COPY {table} ({collist}) FROM STDIN WITH (FORMAT csv)'

    raw = get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.copy_expert(sql, buf)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    return count


def truncate(*tables: str) -> None:
    """Clear staging tables so a re-run does not double-count."""
    raw = get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            for t in tables:
                cur.execute(f"TRUNCATE TABLE {t} RESTART IDENTITY CASCADE")
        raw.commit()
        log.info("truncated %s", ", ".join(tables))
    finally:
        raw.close()


def scalar(sql: str):
    raw = get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        raw.close()

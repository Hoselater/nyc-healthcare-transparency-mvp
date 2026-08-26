#!/usr/bin/env python
"""NYC Healthcare Transparency MVP -- pipeline runner.

Each stage is separately runnable, because they fail for different reasons and
have very different runtimes. A single top-to-bottom script means a throttled
Socrata call forces you to re-crawl every multi-gigabyte MRF.

    python run_pipeline.py schema        # create tables (01_)
    python run_pipeline.py sparcs        # load clinical data
    python run_pipeline.py mrf           # crawl + load pricing data
    python run_pipeline.py transform     # 02_ clinical metrics
    python run_pipeline.py crosswalk     # propose SPARCS <-> CMS mappings
    python run_pipeline.py score         # 03_ + 04_ value index and views
    python run_pipeline.py export        # write the Tableau CSV
    python run_pipeline.py publish       # write the snapshot the web app reads
    python run_pipeline.py all           # everything, in order

Verify the SQL with no data and no Python:
    psql -d postgres -v ON_ERROR_STOP=1 -f sql/99_smoke_test.sql
"""

from __future__ import annotations

import argparse
import logging
import sys

import config


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_schema(_args) -> None:
    from etl import db
    db.run_sql_file(config.SQL_DIR / "01_staging_schema.sql")


def cmd_sparcs(args) -> None:
    from etl import sparcs
    if args.csv:
        sparcs.load_sparcs_csv(args.csv, truncate_first=not args.append)
    else:
        sparcs.fetch_sparcs(truncate_first=not args.append)


def cmd_mrf(args) -> None:
    from etl import cms_hpt, mrf
    locations = cms_hpt.discover_all()
    if not locations:
        logging.error(
            "No MRF locations resolved. Add manual_mrf_url values to %s for the "
            "systems that never deployed cms-hpt.txt.",
            config.TARGET_HOSPITALS_CSV,
        )
        return
    mrf.ingest_all(locations, truncate_first=not args.append)


def cmd_transform(_args) -> None:
    from etl import db
    db.run_sql_file(config.SQL_DIR / "02_transformations.sql")


def cmd_crosswalk(args) -> None:
    from etl import crosswalk
    if args.import_csv:
        crosswalk.import_reviewed(args.import_csv)
    else:
        crosswalk.build()


def cmd_score(_args) -> None:
    from etl import db
    db.run_sql_file(config.SQL_DIR / "03_pricing_and_value_index.sql")
    db.run_sql_file(config.SQL_DIR / "04_export_views.sql")


def cmd_export(args) -> None:
    from etl import export
    export.export_scores(args.out, include_incomplete=args.include_incomplete)


def cmd_publish(_args) -> None:
    from etl import export
    export.publish_snapshot()


def cmd_all(args) -> None:
    cmd_schema(args)
    cmd_sparcs(args)
    cmd_mrf(args)
    cmd_transform(args)
    cmd_crosswalk(args)
    cmd_score(args)
    cmd_export(args)
    cmd_publish(args)
    logging.info("Pipeline complete. Review sql/05_qa_checks.sql before publishing.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("schema", help="create staging tables and config").set_defaults(
        func=cmd_schema
    )

    p_sparcs = sub.add_parser("sparcs", help="load SPARCS clinical data")
    p_sparcs.add_argument("--csv", help="load from a downloaded PUF instead of the API")
    p_sparcs.add_argument("--append", action="store_true",
                          help="keep existing rows instead of truncating")
    p_sparcs.set_defaults(func=cmd_sparcs)

    p_mrf = sub.add_parser("mrf", help="crawl cms-hpt.txt and load MRF pricing")
    p_mrf.add_argument("--append", action="store_true")
    p_mrf.set_defaults(func=cmd_mrf)

    sub.add_parser("transform", help="run 02_transformations.sql").set_defaults(
        func=cmd_transform
    )

    p_xw = sub.add_parser("crosswalk", help="build or import the facility crosswalk")
    p_xw.add_argument("--import-csv", dest="import_csv",
                      help="load a hand-reviewed crosswalk CSV")
    p_xw.set_defaults(func=cmd_crosswalk)

    sub.add_parser("score", help="run 03_ and 04_").set_defaults(func=cmd_score)

    p_export = sub.add_parser("export", help="write the Tableau CSV")
    p_export.add_argument("--out")
    p_export.add_argument("--include-incomplete", action="store_true",
                          help="also export facilities with no published price")
    p_export.set_defaults(func=cmd_export)

    sub.add_parser(
        "publish", help="write data/nyc_ortho_scores_public.csv for the web app"
    ).set_defaults(func=cmd_publish)

    p_all = sub.add_parser("all", help="run every stage in order")
    p_all.add_argument("--csv")
    p_all.add_argument("--append", action="store_true")
    p_all.add_argument("--out")
    p_all.add_argument("--include-incomplete", action="store_true")
    p_all.add_argument("--import-csv", dest="import_csv")
    p_all.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001 - top-level CLI boundary
        logging.error("%s", exc)
        if args.verbose:
            raise
        logging.error("Re-run with -v for a full traceback.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

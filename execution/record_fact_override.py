"""
execution/record_fact_override.py
---------------------------------
Record (or list) a ``fact_overrides`` row — a durable, read-time precedence record
that makes a company-published figure (SEC 8-K / 10-Q / 10-K, earnings press
release, IR deck) supersede FMP's value for one logical fact. The override lives in
the DB, OUTSIDE the FMP cache files, so a re-fetch cannot clobber it.

See ``directives/provenance_override_2026_06.md``.

Examples
--------
  # Segment RECORD-level replace (the GOOG Q4'25 worked example) from a JSON file:
  python execution/record_fact_override.py \
      --ticker GOOG --period-end 2025-12-31 --fiscal-period-type Q4 \
      --fact-kind segment --fact-key product --action replace \
      --value-json-file examples/goog_q4_2025_product_segments.json \
      --source-doc-type sec_8k --source-accession 0001652044-26-000012 \
      --source-exhibit googexhibit991q42025.htm \
      --rationale "8-K exhibit 99.1 supersedes contaminated FMP segmentation"

  # Scalar (financial_fact / kpi) replace:
  python execution/record_fact_override.py \
      --ticker GOOG --period-end 2025-12-31 --fiscal-period-type Q4 \
      --fact-kind kpi --fact-key "Google Cloud revenue growth" --action replace \
      --value 48 --unit percent --source-doc-type ir_press_release

  # List active overrides for a ticker:
  python execution/record_fact_override.py --ticker GOOG --list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import cast

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
sys.path.append(SRC_DIR)

import db  # noqa: E402
from provenance import overrides  # noqa: E402


def _load_value_json(args: argparse.Namespace) -> dict[str, object] | None:
    if args.value_json_file:
        with open(args.value_json_file, encoding="utf-8") as f:
            parsed = json.load(f)
    elif args.value_json:
        parsed = json.loads(args.value_json)
    else:
        return None
    if not isinstance(parsed, dict):
        raise SystemExit("--value-json[-file] must be a JSON object (segment->value map)")
    return cast("dict[str, object]", parsed)


def _cmd_list(conn: object, ticker: str) -> None:
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)
    rows = overrides.get_active_overrides(conn, ticker=ticker)
    if not rows:
        print(f"  no active overrides for {ticker.upper()}")
        return
    print(f"  {len(rows)} active override(s) for {ticker.upper()}:")
    for ov in rows:
        payload = ov.value_json if ov.value_json is not None else ov.value
        print(
            f"    {ov.period_end} {ov.fiscal_period_type:3s} {ov.fact_kind:14s} "
            f"{ov.fact_key:32s} {ov.action:8s} <- {ov.source_doc_type}"
            f"{(' ' + ov.source_accession) if ov.source_accession else ''}"
        )
        print(f"        payload={payload!r} unit={ov.unit}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", required=True)
    parser.add_argument(
        "--list", action="store_true", help="List active overrides for the ticker and exit"
    )
    parser.add_argument("--period-end", help="YYYY-MM-DD")
    parser.add_argument("--fiscal-period-type", help="Q1..Q4 | FY | H1 ...")
    parser.add_argument("--fact-kind", choices=list(overrides.FACT_KINDS))
    parser.add_argument(
        "--fact-key", help="line_item | KPI name | dim_type | 'dim_type|dim_name|metric'"
    )
    parser.add_argument(
        "--action", default="replace", choices=[a.value for a in overrides.OverrideAction]
    )
    parser.add_argument("--value", type=float, help="scalar authoritative value (replace)")
    parser.add_argument("--unit", help="unit of --value (e.g. actual, percent, millions)")
    parser.add_argument("--value-json", help="JSON object for a segment-record replace")
    parser.add_argument(
        "--value-json-file", help="path to a JSON object for a segment-record replace"
    )
    parser.add_argument(
        "--source-doc-type", help="DocType: sec_8k|sec_10q|sec_10k|ir_press_release|..."
    )
    parser.add_argument("--source-accession")
    parser.add_argument("--source-exhibit")
    parser.add_argument("--source-url")
    parser.add_argument("--source-excerpt")
    parser.add_argument("--rationale")
    parser.add_argument("--confidence", type=float, default=1.0)
    parser.add_argument("--created-by", default="cli")
    parser.add_argument("--db-path", help="Target DB (defaults to the project portfolio.db)")
    parser.add_argument("--dry-run", action="store_true", help="Validate + print, do not commit")
    args = parser.parse_args()

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()
    try:
        if args.list:
            _cmd_list(conn, args.ticker)
            return

        missing = [
            name
            for name, val in (
                ("--period-end", args.period_end),
                ("--fiscal-period-type", args.fiscal_period_type),
                ("--fact-kind", args.fact_kind),
                ("--fact-key", args.fact_key),
                ("--source-doc-type", args.source_doc_type),
            )
            if not val
        ]
        if missing:
            raise SystemExit(
                f"missing required args for recording an override: {', '.join(missing)}"
            )

        value_json = _load_value_json(args)
        new_id = overrides.record_override(
            conn,
            ticker=args.ticker,
            period_end=args.period_end,
            fiscal_period_type=args.fiscal_period_type,
            fact_kind=args.fact_kind,
            fact_key=args.fact_key,
            action=args.action,
            value=args.value,
            unit=args.unit,
            value_json=value_json,
            source_doc_type=args.source_doc_type,
            source_accession=args.source_accession,
            source_exhibit=args.source_exhibit,
            source_url=args.source_url,
            source_excerpt=args.source_excerpt,
            confidence=args.confidence,
            rationale=args.rationale,
            created_by=args.created_by,
        )
        if args.dry_run:
            conn.rollback()
            print(
                f"  [dry-run] would record override (rolled back): {args.ticker.upper()} "
                f"{args.period_end} {args.fact_kind}/{args.fact_key} {args.action}"
            )
        else:
            conn.commit()
            print(
                f"  [ok] recorded override id={new_id}: {args.ticker.upper()} "
                f"{args.period_end} {args.fact_kind}/{args.fact_key} {args.action} "
                f"<- {args.source_doc_type}"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

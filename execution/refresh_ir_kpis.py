"""Refresh a ticker's KPI series from its IR historical-data spreadsheet.

The issuer's own quarterly spreadsheet carries clean, audited KPI series. This
ingests them at IR_DOC tier so they supersede the lower-tier LLM brief/press
values in the report (the §3 chart + break-rule loaders prefer the
latest-ingested source per period).

Modes (one required):
  --file PATH    parse + ingest a local spreadsheet
  --url URL      download the spreadsheet, then parse + ingest
  --discover     resolve the current spreadsheet URL via the IR results-center
                 browser adapter, then download + parse + ingest

Examples:
  python execution/refresh_ir_kpis.py --ticker NU --discover --quarters 8
  python execution/refresh_ir_kpis.py --ticker NU --url "https://api.mziq.com/...?origin=2"
  python execution/refresh_ir_kpis.py --ticker NU --file data/ir_spreadsheets/NU/NU_Historical_Data_1Q26.xlsx
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.config import configured_tickers, get_config, save_config  # noqa: E402
from ir_pipeline.download import download_spreadsheet  # noqa: E402
from ir_pipeline.ingest import ingest_spreadsheet_kpis  # noqa: E402
from ir_pipeline.spreadsheet import parse_spreadsheet  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    suppression_payload,
)
from pipeline.source_policy import (  # noqa: E402
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    CollectionSource,
    authorize_stored_collection_target,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = args.db.resolve() if args.db is not None else repo_root / "data" / "portfolio.db"
    bound = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters
    if args.quarters < 1 or args.quarters > bound:
        print(f"--quarters must be between 1 and {bound}", file=sys.stderr)
        return 2
    authorization = authorize_stored_collection_target(
        db_path,
        args.ticker,
        requested=args.owner_requested,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    if not authorization.allowed:
        decision = authorization.decision
        print(
            json.dumps(
                {
                    "event": "source_collection_policy_denied",
                    "ticker": args.ticker.upper(),
                    "source": CollectionSource.IR.value,
                    "artifact_kind": ArtifactKind.IR_DOCUMENT.value,
                    "reason": (
                        decision.reason.value
                        if decision is not None
                        else authorization.status.value
                    ),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    cfg = get_config(args.ticker, repo_root)

    # Resolve the spreadsheet (file / url / discover). --discover needs an existing
    # config (for its results-center URL); a ticker's FIRST refresh must pass
    # --url/--file so the parser config can be generated from the sheet itself.
    if args.file is not None:
        path = args.file
    elif args.url is not None:
        path = download_spreadsheet(args.url, repo_root, args.ticker)
    else:  # --discover
        if cfg is None:
            print(
                f"No IR config for {args.ticker!r} yet — the first refresh must pass "
                "--url/--file (plus --platform / --results-center-url) so the parser "
                f"config can be generated. Configured: {configured_tickers(repo_root)}",
                file=sys.stderr,
            )
            return 2
        from ir_pipeline.discover import discover_spreadsheet_url

        try:
            url = discover_spreadsheet_url(cfg)
        except NotImplementedError as e:
            print(str(e), file=sys.stderr)
            return 3
        if url is None:
            print(f"Could not discover a spreadsheet URL for {args.ticker}.", file=sys.stderr)
            return 4
        path = download_spreadsheet(url, repo_root, args.ticker)

    # Self-generating per-company parser. On a ticker's FIRST refresh the builder
    # maps EVERY labeled numeric row in the just-fetched spreadsheet — the curated
    # holdings tier KPIs (LLM-mapped to canonical names) plus a capture layer for
    # the long tail — and persists it. On a LATER refresh an existing config is
    # WIDENED: its curated analyst rows are preserved verbatim while the capture
    # layer is re-derived from the current sheet (so a hand-built NU.json gains the
    # full audited series without disturbing its four curated KPIs).
    built = False
    if cfg is None:
        from ir_pipeline.config_builder import build_ir_config

        cfg = build_ir_config(
            args.ticker,
            path,
            platform=args.platform or "mz",
            results_center_url=args.results_center_url or "",
            repo_root=repo_root,
        )
        built = True
        if not cfg.spreadsheet_kpis:
            print(
                f"Generated an empty IR config for {args.ticker} — the spreadsheet "
                "carried no capturable numeric rows. See micro_thesis/ir_config/.",
                file=sys.stderr,
            )
            return 5
    else:
        from ir_pipeline.config_builder import widen_config

        cfg = widen_config(cfg, path)
        save_config(cfg, repo_root)

    parsed = parse_spreadsheet(path, cfg, max_quarters=args.quarters)
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        inserted, doc_id = ingest_spreadsheet_kpis(
            conn,
            args.ticker,
            cfg,
            parsed,
            path,
            repo_root=repo_root,
        )
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        return 0
    finally:
        conn.close()

    n_analyst = sum(1 for s in cfg.spreadsheet_kpis if s.origin != "capture")
    n_capture = sum(1 for s in cfg.spreadsheet_kpis if s.origin == "capture")
    print(
        json.dumps(
            {
                "ticker": args.ticker.upper(),
                "config_generated": built,
                "config_analyst_kpis": n_analyst,
                "config_capture_kpis": n_capture,
                "source_file": str(path),
                "doc_id": doc_id,
                "rows_inserted": inserted,
                "kpis": {k: len(v) for k, v in parsed.items()},
            },
            indent=2,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Ticker with an IR config (e.g. NU)")
    bound = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters
    p.add_argument(
        "--quarters",
        type=int,
        default=bound,
        help=f"How many recent reported quarters to ingest (1-{bound})",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="Local spreadsheet to parse + ingest")
    src.add_argument("--url", help="Spreadsheet URL to download, then parse + ingest")
    src.add_argument(
        "--discover",
        action="store_true",
        help="Discover the current spreadsheet URL via the IR results-center browser adapter",
    )
    # Used only when generating a ticker's config on its first refresh.
    p.add_argument("--platform", help="Discovery platform for a new config (mz | q4cdn)")
    p.add_argument(
        "--results-center-url",
        dest="results_center_url",
        help="IR results-center URL stored in a newly generated config (for future --discover)",
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument(
        "--db",
        type=Path,
        help="Caller-selected portfolio DB containing the stored company role",
    )
    p.add_argument(
        "--owner-requested",
        action="store_true",
        help="Explicit owner request; required for evaluation-company collection",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

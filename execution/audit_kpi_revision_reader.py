"""Compare one sourced time-series reader against its revision-aware projection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

try:
    from execution._lib import command_parser
except ModuleNotFoundError:  # managed bootstrap exposes execution/ as the import root
    from _lib import command_parser
from timeseries.kpi_revision_shadow import KpiRevisionReadRequest
from timeseries.loaders import rehearse_kpi_series_reader


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--definition-id", type=int, required=True)
    parser.add_argument("--effective-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--known-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--period-type", action="append", dest="period_types")
    args = parser.parse_args(argv)
    request = KpiRevisionReadRequest.model_validate(
        {
            "ticker": args.ticker,
            "kpi_definition_id": args.definition_id,
            "effective_at": args.effective_at,
            "known_at": args.known_at,
            "period_types": args.period_types or ["Q1", "Q2", "Q3", "Q4"],
        }
    )
    receipt = rehearse_kpi_series_reader(
        db_path=args.db_path, snapshot_manifest=args.snapshot_manifest, request=request
    )
    print(receipt.model_dump_json())
    return 2  # a comparison receipt always remains HOLD, even when values agree


if __name__ == "__main__":
    raise SystemExit(main())

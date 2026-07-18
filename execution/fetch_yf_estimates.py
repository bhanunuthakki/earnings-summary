"""execution/fetch_yf_estimates.py — yfinance forward-consensus estimates fetcher.

Persists the free Yahoo analysis tables (``earnings_estimate``,
``revenue_estimate``, ``growth_estimates``, ``eps_trend``, ``eps_revisions``,
``analyst_price_targets``) per tracked ticker — the only free forward REVENUE
consensus (FMP Starter's analyst-estimates truncates to 10 rows/call) plus
7/30/60/90-day revision drift.

Storage MIRRORS the FMP pattern in ``execution/save_fmp_data.py``:

  data/historical/yfinance/<TICKER>_yf_estimates.json             latest pull
  data/historical/yfinance_snapshots/<YYYY-MM-DD>/<same name>     point-in-time

Forward consensus is time-sensitive by nature, so EVERY successful pull is
snapshotted (the analog of save_fmp_data's TIME_SENSITIVE_ENDPOINTS treatment)
— the dated archive is what makes revenue-surprise reconstruction and as-of
reads possible later.

Degrade contract (repo per-item pattern): yfinance is an unofficial API — each
ticker is wrapped in its own try/except; a failed or empty ticker is tallied
and deferred to the next run, never aborts the batch, and the CLI exits 0. A
run where NOTHING could be fetched still exits 0 with the tally (the caller's
cron log shows failed=N). Idempotent: a ticker whose snapshot for --asof
already exists is skipped ("already done") unless --force.

Usage:
    python execution/fetch_yf_estimates.py                       # active universe
    python execution/fetch_yf_estimates.py --tickers META NOW
    python execution/fetch_yf_estimates.py --repo-root C:/path/to/main/repo
    python execution/fetch_yf_estimates.py --force
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from models.yf_payloads import (  # noqa: E402
    YfEpsRevisionsRow,
    YfEpsTrendRow,
    YfEstimateRow,
    YfEstimatesSnapshot,
    YfGrowthRow,
    YfPriceTargets,
)

YF_FILE_SUFFIX = "_yf_estimates.json"

#: (snapshot field, yfinance Ticker attribute) — the five frame-shaped tables.
_FRAME_TABLES: tuple[tuple[str, str], ...] = (
    ("earnings_estimate", "earnings_estimate"),
    ("revenue_estimate", "revenue_estimate"),
    ("growth_estimates", "growth_estimates"),
    ("eps_trend", "eps_trend"),
    ("eps_revisions", "eps_revisions"),
)

_ROW_MODELS: dict[
    str, type[YfEstimateRow] | type[YfGrowthRow] | type[YfEpsTrendRow] | type[YfEpsRevisionsRow]
] = {
    "earnings_estimate": YfEstimateRow,
    "revenue_estimate": YfEstimateRow,
    "growth_estimates": YfGrowthRow,
    "eps_trend": YfEpsTrendRow,
    "eps_revisions": YfEpsRevisionsRow,
}

RawTables = dict[str, list[dict[str, object]]]
TablesLoader = Callable[[str], RawTables]


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


# ---------------------------------------------------------------------------
# yfinance access (duck-typed; tests inject tables and never import yfinance)
# ---------------------------------------------------------------------------


def _frame_to_records(frame: object) -> list[dict[str, object]]:
    """DataFrame -> plain record dicts with the ``period`` index restored as a
    column. Duck-typed via getattr (same idiom as execution/fetch_yf_grades.py)
    so the module imports without pandas; any shape surprise degrades to []."""
    if frame is None:
        return []
    try:
        reset_index = getattr(frame, "reset_index", None)
        if not callable(reset_index):
            return []
        to_dict = getattr(reset_index(), "to_dict", None)
        if not callable(to_dict):
            return []
        records: object = to_dict("records")
    except Exception:
        return []
    if not isinstance(records, list):
        return []
    out: list[dict[str, object]] = []
    for rec in cast("list[object]", records):
        if isinstance(rec, dict):
            row = {str(k): v for k, v in cast("dict[object, object]", rec).items()}
            # An unnamed index resets to a column literally called "index".
            if "period" not in row and "index" in row:
                row["period"] = row.pop("index")
            out.append(row)
    return out


def _load_yf_tables(ticker: str) -> RawTables:
    """Live yfinance pull -> {table_name: record dicts}. Per-table degrade: a
    table that raises or drifts contributes [] and the rest still return.
    Raises only if yfinance itself is unusable (caller's per-ticker guard)."""
    import yfinance as yf  # type: ignore[import-untyped]

    tkr = yf.Ticker(ticker)
    tables: RawTables = {}
    for field_name, attr in _FRAME_TABLES:
        try:
            frame = cast("object", getattr(tkr, attr))
        except Exception:
            tables[field_name] = []
            continue
        tables[field_name] = _frame_to_records(frame)
    try:
        targets = cast("object", tkr.analyst_price_targets)
    except Exception:
        targets = None
    if isinstance(targets, dict):
        tables["analyst_price_targets"] = [
            {str(k): v for k, v in cast("dict[object, object]", targets).items()}
        ]
    else:
        tables["analyst_price_targets"] = []
    return tables


# ---------------------------------------------------------------------------
# Mapping + validation (pure; the test surface)
# ---------------------------------------------------------------------------


def build_snapshot(
    ticker: str, tables: RawTables, *, asof_date: str, fetched_at: str
) -> YfEstimatesSnapshot:
    """Validate raw table records into a ``YfEstimatesSnapshot``. A row that
    fails its model is dropped individually (logged), never padded — a Yahoo
    column rename degrades that table, not the snapshot."""
    validated: dict[str, list[object]] = {}
    for field_name, _ in _FRAME_TABLES:
        model = _ROW_MODELS[field_name]
        rows: list[object] = []
        for raw in tables.get(field_name, []):
            try:
                rows.append(model.model_validate(raw))
            except ValidationError:
                _log("yf_estimates_row_rejected", ticker=ticker, table=field_name)
        validated[field_name] = rows
    targets: YfPriceTargets | None = None
    raw_targets = tables.get("analyst_price_targets", [])
    if raw_targets:
        try:
            targets = YfPriceTargets.model_validate(raw_targets[0])
        except ValidationError:
            _log("yf_estimates_row_rejected", ticker=ticker, table="analyst_price_targets")
    return YfEstimatesSnapshot(
        ticker=ticker,
        asof_date=asof_date,
        fetched_at=fetched_at,
        earnings_estimate=cast("list[YfEstimateRow]", validated["earnings_estimate"]),
        revenue_estimate=cast("list[YfEstimateRow]", validated["revenue_estimate"]),
        growth_estimates=cast("list[YfGrowthRow]", validated["growth_estimates"]),
        eps_trend=cast("list[YfEpsTrendRow]", validated["eps_trend"]),
        eps_revisions=cast("list[YfEpsRevisionsRow]", validated["eps_revisions"]),
        analyst_price_targets=targets,
    )


def _write_snapshot(snapshot: YfEstimatesSnapshot, latest_dir: Path, snap_dir: Path) -> None:
    """Write latest + dated snapshot atomically (tmp-then-replace)."""
    payload = json.dumps(snapshot.model_dump(), indent=2)
    for dest_dir in (latest_dir, snap_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"{snapshot.ticker}{YF_FILE_SUFFIX}"
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(out)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(
    tickers: list[str],
    *,
    data_root: Path,
    asof_date: str,
    force: bool = False,
    tables_loader: TablesLoader | None = None,
) -> dict[str, int]:
    """Fetch + persist every ticker with per-item degrade. Returns the tally
    ``{ok, skipped, empty, failed}``. ``tables_loader`` is the test seam."""
    loader = tables_loader or _load_yf_tables
    latest_dir = data_root / "historical" / "yfinance"
    snap_root = data_root / "historical" / "yfinance_snapshots"
    snap_dir = snap_root / asof_date
    tally = {"ok": 0, "skipped": 0, "empty": 0, "failed": 0}
    deferred: list[str] = []
    for ticker in tickers:
        ticker = ticker.upper()
        snap_file = snap_dir / f"{ticker}{YF_FILE_SUFFIX}"
        if snap_file.exists() and not force:
            tally["skipped"] += 1
            _log("yf_estimates_already_done", ticker=ticker, asof=asof_date)
            continue
        try:
            tables = loader(ticker)
        except Exception as exc:
            tally["failed"] += 1
            deferred.append(ticker)
            _log(
                "yf_estimates_fetch_failed",
                ticker=ticker,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            continue
        snapshot = build_snapshot(
            ticker,
            tables,
            asof_date=asof_date,
            fetched_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        )
        present = snapshot.table_names_present()
        if not present:
            # Nothing usable came back (thin coverage or a transient Yahoo
            # outage): tally + defer, don't write an empty snapshot that would
            # then be skipped as "already done" forever.
            tally["empty"] += 1
            deferred.append(ticker)
            _log("yf_estimates_empty", ticker=ticker)
            continue
        _write_snapshot(snapshot, latest_dir, snap_dir)
        tally["ok"] += 1
        _log("yf_estimates_saved", ticker=ticker, asof=asof_date, tables=present)
    _log("yf_estimates_done", asof=asof_date, deferred=deferred, **tally)
    return tally


def _resolve_tickers(db_path: str, arg_tickers: list[str] | None) -> list[str]:
    """Active tracked universe (portfolio/watchlist/evaluation, non-archived) —
    same selection rule as execution/backfill_earnings_surprises.py.
    ``db.get_connection`` resolves from the module-level ``db.DB_PATH``, so a
    ``--db-path``/``--repo-root`` override re-points it via ``db.set_db_path``
    (the repo's standard convention for CLIs that accept ``--db-path``)."""
    if arg_tickers:
        return [t.upper() for t in arg_tickers]
    db.set_db_path(db_path)
    conn = db.get_connection()
    try:
        cur = conn.execute(
            f"SELECT ticker FROM tracked_companies "
            f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} "
            f"AND archived_at IS NULL ORDER BY ticker"
        )
        return [str(row["ticker"]) for row in cur.fetchall()]
    finally:
        conn.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", nargs="*", help="Whitespace-separated tickers (default: active universe)."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/. Worktree-based runs pass the main repo path.",
    )
    parser.add_argument("--db-path", default=None, help="Override the portfolio DB path.")
    parser.add_argument(
        "--asof",
        default=None,
        help="Snapshot date YYYY-MM-DD (default: today). The idempotency key.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch tickers already snapshotted for --asof."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = cast("Path", args.repo_root).resolve()
    db_path = cast("str | None", args.db_path) or str(repo_root / "data" / "portfolio.db")
    asof_date = cast("str | None", args.asof) or datetime.now().strftime("%Y-%m-%d")
    tickers = _resolve_tickers(db_path, cast("list[str] | None", args.tickers))
    if not tickers:
        _log("yf_estimates_no_tickers")
        print(json.dumps({"ok": 0, "skipped": 0, "empty": 0, "failed": 0}))
        return 0
    tally = run(
        tickers,
        data_root=repo_root / "data",
        asof_date=asof_date,
        force=cast("bool", args.force),
    )
    print(json.dumps(tally))
    return 0


if __name__ == "__main__":
    sys.exit(main())

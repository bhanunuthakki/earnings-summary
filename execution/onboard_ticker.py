"""Onboard a newly-tracked ticker: fetch FMP fundamentals, then run parse stages.

Bridges the gap between `db.track_company` (a snappy DB upsert) and the rest of
the pipeline (network-bound fetches + parse). Designed to be invoked as a
fire-and-forget subprocess from `db.track_company` when a ticker is added to
the portfolio, watchlist, or evaluation list (`db.ACTIVE_LIST_TYPES`) — see
the call site there.

Sequence per ticker:
    0. (Optional) Apply industry KPI template via --industry-template <slug>
       or --industry-template auto. Merges template canonical_kpis into
       micro_thesis/holdings/<T>.json (preserving any existing tier_1_kpis),
       seeds the company entity row with sector metadata, and sets
       tracked_companies.processing_tier (P1/P2/P3). See
       `apply_industry_template` below.
    1. FMP fetch via `execution/save_fmp_data.py --tickers TICKER --skip-existing`
       (subprocess; the script has its own DB-connection lifecycle and resumes
       cleanly via fmp_endpoint_status).
    2. Quarterly refresh via `pipeline.quarterly_refresh.refresh_ticker`
       (in-process; idempotent across all 7 stages).
    3. Transcript backfill via `execution/backfill_transcripts.py --ticker X`
       (subprocess; fetches the last 6 fiscal quarters of Q&A from the free
       aggregator chain, runs ingest, and extracts forward-looking
       commitments). Tolerates aggregator misses — they don't fail the
       onboard. The daily cron `cron/backfill_transcripts.task.xml` covers
       the same ground for the full active universe on a daily cadence.

Usage:
    python execution/onboard_ticker.py --ticker BKNG
    python execution/onboard_ticker.py --ticker BKNG --skip-fmp           # parse-only
    python execution/onboard_ticker.py --ticker BKNG --skip-transcripts   # no aggregator fetch
    python execution/onboard_ticker.py --ticker CRWD --industry-template software_saas
    python execution/onboard_ticker.py --ticker NU --industry-template auto

This is the FMP + aggregator-transcript onboard path. SEC XBRL, IR-doc, and
audio fallback fetches stay explicit user actions per
`directives/data_pipeline_dag.md`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from entity_store import upsert_entity  # noqa: E402
from industry_classifier import (  # noqa: E402
    IndustryTemplate,
    classify_ticker,
    load_template,
)
from models.runs import StageStatus as RunStageStatus  # noqa: E402
from pipeline.fmp_doc_index import (  # noqa: E402
    index_fmp_files_for_ticker,
    set_filing_regime_from_profile,
    set_fiscal_year_end_from_fmp,
    set_instrument_type_from_fmp,
)
from pipeline.invocation_fingerprint import files_fingerprint  # noqa: E402
from pipeline.quarterly_refresh import (  # noqa: E402
    StageStatus as RefreshStageStatus,
)
from pipeline.quarterly_refresh import refresh_ticker  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    start_run,
    suppression_payload,
)
from pipeline.transcript_acquisition import (  # noqa: E402
    persist_authorized_transcript_artifact,
    read_authorized_transcript,
    stage_pending_issuer_transcripts,
)
from runtime.python_process import managed_python_prefix  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from transcripts.acquisition_semantics import TranscriptAcquisitionEntrypoint  # noqa: E402

log = logging.getLogger(__name__)

_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
_FMP_SCRIPT = PROJECT_ROOT / "execution" / "save_fmp_data.py"
_BACKFILL_SCRIPT = PROJECT_ROOT / "execution" / "backfill_transcripts.py"

# Mapping from list_type → processing tier. Used by the template applier when
# it sets tracked_companies.processing_tier. Mirrors the rules in migration
# 0044's backfill so the two stay in sync.
_LIST_TYPE_TO_TIER: dict[str, str] = {
    "portfolio": "P1",
    "watchlist": "P2",
    "evaluation": "P2",
    "etf": "P3",
    "index_member": "P3",
    "none": "P3",
}


def _onboard_invocation_inputs(
    args: argparse.Namespace,
    ticker: str,
    *,
    instrument: str | None,
) -> dict[str, JsonValue]:
    """Material files and behavior flags for the accounted onboarding run."""
    fmp_dir = PROJECT_ROOT / "data" / "historical" / "fmp"
    material_files = [
        _HOLDINGS_DIR / f"{ticker}.json",
        *sorted(fmp_dir.glob(f"{ticker}_*")),
    ]
    return {
        "files": files_fingerprint(material_files, root=PROJECT_ROOT),
        "skip_fmp": bool(args.skip_fmp),
        "skip_transcripts": bool(args.skip_transcripts),
        "skip_ir": bool(args.skip_ir),
        "skip_saydo": bool(args.skip_saydo),
        "force_saydo": bool(args.force_saydo),
        "industry_template": args.industry_template,
        "instrument_override": args.instrument,
        "resolved_instrument": instrument,
    }


# ---------------------------------------------------------------------------
# Industry template application
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TemplateApplyResult:
    """What happened when the template was applied."""

    industry_slug: str
    holdings_path: Path
    holdings_written: bool
    kpis_added: list[str]
    kpis_kept: list[str]
    entity_id: int | None
    processing_tier: str | None


def apply_industry_template(
    *,
    ticker: str,
    industry_slug: str,
    repo_root: Path,
    holdings_dir: Path | None = None,
    db_path: Path | None = None,
) -> TemplateApplyResult:
    """Apply an industry template to a ticker. Idempotent + safe to re-run.

    Steps:
      1) Load templates/industry/<slug>.yaml
      2) Merge canonical_kpis into micro_thesis/holdings/<TICKER>.json's
         tier_1_kpis (existing entries by name are preserved verbatim;
         only template KPIs whose name isn't already present get appended).
      3) Upsert a company entity row via entity_store.upsert_entity, with
         external_ids={'ticker': ...} and meta={'sector': <from seed>} when
         the ticker is in the entity_seed registry.
      4) Set tracked_companies.processing_tier based on list_type. Falls
         back to 'P3' when the ticker isn't in tracked_companies yet (a
         fresh onboard before track_company has fired wouldn't have the row).

    Each step degrades gracefully — a missing DB doesn't prevent the holdings
    JSON from being written, and vice versa. Callers can inspect the returned
    TemplateApplyResult to see what actually happened.
    """
    holdings_dir_resolved = holdings_dir or (repo_root / "micro_thesis" / "holdings")
    db_path_resolved = db_path or (repo_root / "data" / "portfolio.db")
    holdings_dir_resolved.mkdir(parents=True, exist_ok=True)

    template = load_template(industry_slug, repo_root)
    holdings_path = holdings_dir_resolved / f"{ticker.upper()}.json"
    holdings, was_existing = _load_or_init_holdings(
        ticker=ticker,
        holdings_path=holdings_path,
        template=template,
    )
    added, kept = _merge_tier_1_kpis(holdings, template)
    _stamp_template_meta(holdings, template)
    holdings["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    holdings_path.write_text(
        json.dumps(holdings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    entity_id = _seed_company_entity(
        ticker=ticker.upper(),
        db_path=db_path_resolved,
    )
    processing_tier = _set_processing_tier(
        ticker=ticker.upper(),
        db_path=db_path_resolved,
    )

    log.info(
        {
            "event": "industry_template_applied",
            "ticker": ticker.upper(),
            "industry": template.industry,
            "holdings_existed": was_existing,
            "kpis_added": added,
            "kpis_kept_count": len(kept),
            "entity_id": entity_id,
            "processing_tier": processing_tier,
        },
    )

    return TemplateApplyResult(
        industry_slug=template.industry,
        holdings_path=holdings_path,
        holdings_written=True,
        kpis_added=added,
        kpis_kept=kept,
        entity_id=entity_id,
        processing_tier=processing_tier,
    )


def _load_or_init_holdings(
    *,
    ticker: str,
    holdings_path: Path,
    template: IndustryTemplate,
) -> tuple[dict[str, object], bool]:
    """Return (holdings_dict, was_existing). Initializes a minimal stub when
    the file doesn't exist — same shape as the recurring-onboard stubs (see
    e.g. CRWD.json before this run): just enough fields to keep downstream
    readers happy."""
    if holdings_path.exists():
        raw = holdings_path.read_text(encoding="utf-8")
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{holdings_path.name} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{holdings_path.name} is not a JSON object")
        return (cast("dict[str, object]", loaded), True)
    stub: dict[str, object] = {
        "ticker": ticker.upper(),
        "name": ticker.upper(),
        "thesis": (
            f"{ticker.upper()} — onboarded via {template.industry} industry template. "
            f"STUB: needs user-authored thesis."
        ),
        "verdict": "Pending",
        "verdict_color": "gray",
        "key_driver": "TBV — set during thesis authoring",
        "_status": "stub_for_recurring_onboard",
        "tier_1_kpis": [],
        "tier_2_kpis": [],
        "tier_3_kpis": [],
        "break_rules": [],
        "schema_version": 2,
        "wacc": None,
        "mos_bar": None,
        "dcf_defaults": {"forecast_years": 5, "terminal_multiple": None},
        "flags": [],
        "segments": [],
        "operational_kpis": [],
        "break_rules_soft": [],
    }
    return (stub, False)


def _merge_tier_1_kpis(
    holdings: dict[str, object],
    template: IndustryTemplate,
) -> tuple[list[str], list[str]]:
    """Merge template canonical_kpis into holdings.tier_1_kpis. Returns
    (added_names, kept_existing_names). Existing entries (matched by case-
    insensitive name OR by name being one of the template KPI's aliases) are
    preserved verbatim — the user's prior thresholds and TBV values stay."""
    existing_raw = holdings.get("tier_1_kpis")
    if not isinstance(existing_raw, list):
        existing_raw = []
    existing = cast("list[object]", existing_raw)

    existing_names_lc: set[str] = set()
    kept: list[str] = []
    for item in existing:
        if isinstance(item, dict):
            item_dict = cast("dict[str, object]", item)
            n = item_dict.get("name")
            if isinstance(n, str):
                existing_names_lc.add(n.lower())
                kept.append(n)

    new_rows = template.to_tier_1_kpis()
    added: list[str] = []
    for row in new_rows:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        # Skip if the canonical name OR any of the template's aliases is
        # already present (handles "NDR" written as "Net Revenue Retention"
        # in an existing user-authored row).
        candidate_keys = {name.lower()}
        aliases_obj = row.get("aliases")
        if isinstance(aliases_obj, list):
            for a in cast("list[object]", aliases_obj):
                if isinstance(a, str):
                    candidate_keys.add(a.lower())
        if existing_names_lc.intersection(candidate_keys):
            continue
        existing.append(row)
        existing_names_lc.add(name.lower())
        added.append(name)

    holdings["tier_1_kpis"] = existing
    return (added, kept)


def _stamp_template_meta(holdings: dict[str, object], template: IndustryTemplate) -> None:
    """Record which template was applied so future re-runs are diff-able."""
    holdings["industry_template"] = template.industry
    holdings["industry_template_display"] = template.display_name


def _seed_company_entity(*, ticker: str, db_path: Path) -> int | None:
    """Create the company entity row (idempotent). Returns entity_id or None."""
    sector, canonical_name, display_name = _lookup_seed(ticker)
    return upsert_entity(
        kind="company",
        canonical_name=canonical_name or ticker,
        display_name=display_name or ticker,
        external_ids={"ticker": ticker},
        meta={"sector": sector} if sector else None,
        db_path=db_path,
    )


def _lookup_seed(ticker: str) -> tuple[str | None, str | None, str | None]:
    """Return (sector, canonical_name, display_name) from entity_seed; missing
    fields come back as None. Imported lazily so the module loads even if
    entity_seed has cyclic-import issues at the moment of CLI startup."""
    try:
        from entity_seed import PORTFOLIO_HOLDINGS, WATCHLIST_BIZ_MODELS
    except ImportError:
        return (None, None, None)
    if ticker in PORTFOLIO_HOLDINGS:
        seed = PORTFOLIO_HOLDINGS[ticker]
        return (seed.sector, seed.canonical_name, seed.display_name)
    if ticker in WATCHLIST_BIZ_MODELS:
        meta = WATCHLIST_BIZ_MODELS[ticker]
        return (meta.get("sector"), meta.get("name"), meta.get("display"))
    return (None, None, None)


def _set_processing_tier(*, ticker: str, db_path: Path) -> str | None:
    """Update tracked_companies.processing_tier based on current list_type.
    Returns the tier value set, or None if the row / column doesn't exist."""
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        conn.row_factory = sqlite3.Row
        # Bail if the column isn't present (migration 0044 hasn't run yet)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tracked_companies)").fetchall()}
        if "processing_tier" not in cols:
            conn.close()
            return None
        row = conn.execute(
            "SELECT list_type FROM tracked_companies WHERE ticker = ? LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        tier = _LIST_TYPE_TO_TIER.get(row["list_type"], "P3")
        conn.execute(
            "UPDATE tracked_companies SET processing_tier = ? WHERE ticker = ?",
            (tier, ticker),
        )
        conn.commit()
        conn.close()
        return tier
    except sqlite3.Error as exc:
        log.warning({"event": "set_processing_tier_failed", "error": str(exc)})
        return None


# ---------------------------------------------------------------------------
# Subprocess wrappers (unchanged)
# ---------------------------------------------------------------------------


def _run_fmp_fetch(ticker: str) -> int:
    """Invoke save_fmp_data.py as a subprocess; return its exit code."""
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(_FMP_SCRIPT),
        "--tickers",
        ticker,
        "--skip-existing",
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return proc.returncode


def _run_transcript_backfill(ticker: str) -> int:
    """Invoke backfill_transcripts.py for a single ticker.

    Fetches recent Q&A transcripts from free aggregators, ingests them, and
    extracts commitments. Tolerates aggregator coverage gaps.
    """
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(_BACKFILL_SCRIPT),
        "--ticker",
        ticker,
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return proc.returncode


def _run_ir_documents(ticker: str) -> int:
    """Discover + fetch + register + process IR documents for a single ticker.

    Best-effort day-one coverage on onboard via the SHARED single-ticker chain
    (the same ``run_ticker`` entry the weekly batch + failing-crawler rescan use):
    headless-crawl the IR site, download + content-classify + register the docs,
    feed them into the ``--enable-llm`` pipeline (ir_narrative anchor + brief_dirty),
    and record the outcome in ``ir_fetch_status`` so the dashboard surfaces a freshly
    onboarded name immediately — no wait for the weekly roster sweep. The shared
    chain derives the fiscal calendar from ``fiscal_year_end`` itself.

    Needs the optional ``ir`` extra for the headless crawl; a missing extra just
    makes the discover child exit non-zero, which the shared chain tolerates
    (returns a FAILED result rather than raising). Bounded by the chain's per-stage
    timeouts, so a hung browser cannot stall the onboard indefinitely.
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from execution.discover_ir_documents_all import TickerStatus, run_ticker

    result = run_ticker(
        ticker,
        repo_root=PROJECT_ROOT,
        db_path=_DB_PATH,
        process=True,
        owner_requested=True,
    )
    return 0 if result.status is not TickerStatus.FAILED else 1


def _lookup_list_type(ticker: str) -> str | None:
    """Return tracked_companies.list_type for the ticker, or None if absent.

    Mirrors the list_type read in `_set_processing_tier`. Used to gate the
    Say-Do onboarding step to evaluation-list names.
    """
    if not _DB_PATH.exists():
        return None
    try:
        conn = connect_sqlite(str(_DB_PATH), role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT list_type FROM tracked_companies WHERE ticker = ? LIMIT 1",
            (ticker,),
        ).fetchone()
        conn.close()
        return row["list_type"] if row is not None else None
    except sqlite3.Error as exc:
        log.warning({"event": "lookup_list_type_failed", "ticker": ticker, "error": str(exc)})
        return None


def _run_saydo(ticker: str) -> int:
    """Generate per-quarter transcript summaries + Say-Do pairs for a ticker.

    Closes the gap that leaves §6 Say-Do empty for freshly-onboarded names:
    transcripts are registered processed=True at ingest without a summary ever
    being written, so the normal pipeline never summarizes them. This runs
    `process_ir_documents.py --regenerate-missing` (cache-aware — only writes
    summaries that are absent, never re-bills existing ones) then
    `build_saydo_pairs.py` to pair consecutive quarters into the §6 cards.

    Returns the first non-zero subprocess exit code, or 0 on success.
    """
    process_ir = PROJECT_ROOT / "execution" / "process_ir_documents.py"
    build_saydo = PROJECT_ROOT / "execution" / "build_saydo_pairs.py"
    rc = subprocess.run(
        [
            *managed_python_prefix(PROJECT_ROOT),
            str(process_ir),
            "--ticker",
            ticker,
            "--regenerate-missing",
        ],
        cwd=str(PROJECT_ROOT),
    ).returncode
    if rc != 0:
        return rc
    return subprocess.run(
        [*managed_python_prefix(PROJECT_ROOT), str(build_saydo), "--ticker", ticker],
        cwd=str(PROJECT_ROOT),
    ).returncode


def _saydo_should_run(list_type: str | None, *, force: bool, instrument: str | None = None) -> bool:
    """Say-Do generation runs for evaluation-list names (the gap this closes)
    or for any name when forced (e.g. backfilling an already-onboarded ticker).

    Never for ETFs — funds hold no earnings calls, so there are no
    management commitments to pair. Belt-and-braces: the ETF onboarding
    branch in ``main`` returns before this gate is ever consulted, but a
    ``--force-saydo`` on an ETF must still be a no-op.
    """
    if instrument == "etf":
        return False
    return force or list_type == "evaluation"


def run_etf_onboarding(conn: sqlite3.Connection, ticker: str, repo_root: Path) -> int:
    """The ETF onboarding-lite path (directives/etf_data.md): published data
    only — N-PORT holdings, issuer overlay, yfinance price history. No
    quarterly refresh, no transcripts, no IR crawl, no Say-Do: none of the
    bottoms-up equity stages mean anything for a fund.

    Returns 0 when at least one holdings source landed, 1 when both degraded
    (evaluation analytics will be partial until a source succeeds), 2 on an
    N-PORT schema-drift halt.
    """
    from etf_sources.ingest import refresh_published_data
    from etf_sources.nport import NportParseError

    print(f"[onboard] {ticker} stage=etf_published_data", flush=True)
    try:
        result = refresh_published_data(conn, ticker, repo_root)
    except NportParseError as exc:
        print(f"[onboard] {ticker} etf_published_data NPORT PARSE HALT: {exc}", flush=True)
        return 2
    conn.commit()
    as_of = result.nport_as_of.isoformat() if result.nport_as_of else "-"
    print(
        f"[onboard] {ticker} etf nport={result.nport_status} as_of={as_of} "
        f"rows={result.nport_rows} issuer={result.issuer_status} "
        f"issuer_rows={result.issuer_rows} "
        f"characteristics={'yes' if result.characteristics_applied else 'no'} "
        f"prices={result.price_status} price_rows={result.price_rows}",
        flush=True,
    )
    for skipped in ("quarterly_refresh", "backfill_transcripts", "ir_documents", "saydo"):
        print(f"[onboard] {ticker} stage={skipped} SKIPPED (instrument_type=etf)", flush=True)
    # Role-in-portfolio one-pager: one governed LLM call, sha-cached, best
    # effort (a fresh ETF lands with its workup; a failure never fails the
    # onboard — the workup peek shows the build-hint CLI instead).
    print(f"[onboard] {ticker} stage=etf_role_synthesis", flush=True)
    workup_rc = subprocess.run(
        [
            *managed_python_prefix(PROJECT_ROOT),
            str(PROJECT_ROOT / "execution" / "build_etf_workup.py"),
            "--ticker",
            ticker,
        ],
        cwd=str(PROJECT_ROOT),
    ).returncode
    if workup_rc != 0:
        print(
            f"[onboard] {ticker} etf_role_synthesis rc={workup_rc}; continuing (best-effort)",
            flush=True,
        )
    if result.nport_status == "unavailable" and result.issuer_status == "unavailable":
        print(
            f"[onboard] {ticker} WARNING: no ETF holdings source succeeded — "
            f"look-through analytics degrade until one does",
            flush=True,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True, help="Ticker to onboard (e.g. BKNG)")
    ap.add_argument(
        "--skip-fmp",
        action="store_true",
        help="Skip the FMP fetch step (parse-only; useful for re-runs)",
    )
    ap.add_argument(
        "--skip-transcripts",
        action="store_true",
        help="Skip the aggregator-transcript backfill step (faster onboard for re-runs)",
    )
    ap.add_argument(
        "--skip-ir",
        action="store_true",
        help="Skip the IR-document discover+fetch step (e.g. no `ir` extra installed)",
    )
    ap.add_argument(
        "--skip-saydo",
        action="store_true",
        help="Skip the Say-Do step (per-quarter transcript summaries + pairwise cards).",
    )
    ap.add_argument(
        "--force-saydo",
        action="store_true",
        help="Run the Say-Do step regardless of list_type (default: evaluation "
        "names only). Useful for backfilling an already-onboarded name.",
    )
    ap.add_argument(
        "--industry-template",
        default=None,
        help=(
            "Apply an industry KPI template before the pipeline runs. Pass "
            "a slug (software_saas, bank, pharma, commodity_royalty, "
            "hyperscaler) or 'auto' to let the deterministic classifier "
            "pick one. Omitting this flag preserves the prior generic-onboard "
            "behavior."
        ),
    )
    ap.add_argument(
        "--instrument",
        choices=["etf", "equity", "adr"],
        default=None,
        help=(
            "Force tracked_companies.instrument_type before classification. "
            "Escape hatch for symbols whose FMP profile is unavailable (the "
            "auto-classifier needs it); an ETF routes to the published-data "
            "onboarding-lite path."
        ),
    )
    args = ap.parse_args()
    ticker = args.ticker.upper()

    started = datetime.now()
    print(f"[onboard] {ticker} starting at {started.isoformat(timespec='seconds')}", flush=True)

    industry_arg = args.industry_template
    if industry_arg:
        slug = classify_ticker(ticker, PROJECT_ROOT) if industry_arg == "auto" else industry_arg
        if slug is None:
            print(
                f"[onboard] {ticker} --industry-template=auto found no match; "
                f"continuing without template application",
                flush=True,
            )
        else:
            try:
                result = apply_industry_template(
                    ticker=ticker,
                    industry_slug=slug,
                    repo_root=PROJECT_ROOT,
                    holdings_dir=_HOLDINGS_DIR,
                    db_path=_DB_PATH,
                )
            except (FileNotFoundError, ValueError) as exc:
                print(
                    f"[onboard] {ticker} industry-template apply FAILED: {exc}",
                    flush=True,
                )
                return 2
            else:
                print(
                    f"[onboard] {ticker} stage=industry_template "
                    f"slug={result.industry_slug} "
                    f"kpis_added={len(result.kpis_added)} "
                    f"kpis_kept={len(result.kpis_kept)} "
                    f"entity_id={result.entity_id} "
                    f"tier={result.processing_tier}",
                    flush=True,
                )

    if not args.skip_fmp:
        print(f"[onboard] {ticker} stage=fmp_fetch", flush=True)
        rc = _run_fmp_fetch(ticker)
        if rc != 0:
            print(f"[onboard] {ticker} fmp_fetch FAILED (rc={rc}); continuing to parse", flush=True)

    conn = open_db(_DB_PATH)
    try:
        print(f"[onboard] {ticker} stage=index_fmp_documents", flush=True)
        n_indexed = index_fmp_files_for_ticker(conn, ticker, PROJECT_ROOT)
        print(f"[onboard] {ticker} indexed {n_indexed} new fmp documents rows", flush=True)

        print(f"[onboard] {ticker} stage=set_fiscal_year_end", flush=True)
        fye = set_fiscal_year_end_from_fmp(conn, ticker, PROJECT_ROOT)
        print(f"[onboard] {ticker} fiscal_year_end={fye!s}", flush=True)

        # Classify instrument_type from the FMP profile (only when NULL) so
        # direct/raw-SQL onboards don't sit at NULL forever and trip the
        # 'no_instrument_type' pending reason. See set_instrument_type_from_fmp.
        print(f"[onboard] {ticker} stage=set_instrument_type", flush=True)
        if args.instrument:
            conn.execute(
                "UPDATE tracked_companies SET instrument_type = ? WHERE ticker = ?",
                (args.instrument, ticker),
            )
            conn.commit()
        instrument = set_instrument_type_from_fmp(conn, ticker, PROJECT_ROOT)
        if instrument is None:
            # No FMP profile cache (plan-gated symbol / --skip-fmp): the
            # classifier can't answer, but the COLUMN may already carry the
            # kind (the --instrument override above, db.track_company's
            # curated path, or a prior run) — read it directly, or the ETF
            # branch below silently misses and the fund takes the equity
            # pipeline (caught by the AVDV end-to-end).
            row = conn.execute(
                "SELECT instrument_type FROM tracked_companies WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            instrument = row[0] if row and row[0] else None
        print(f"[onboard] {ticker} instrument_type={instrument!s}", flush=True)

        # Self-heal filing_regime the same way (write-only-when-NULL; never
        # clobbers the hand-curated 0001 backfill). segment_quarterly_framework.md
        # §1.3 — same call site as set_instrument_type_from_fmp above.
        print(f"[onboard] {ticker} stage=set_filing_regime", flush=True)
        filing_regime = set_filing_regime_from_profile(conn, ticker, PROJECT_ROOT)
        print(f"[onboard] {ticker} filing_regime={filing_regime!s}", flush=True)

        # ETFs take the published-data onboarding-lite path: the remaining
        # stages (quarterly refresh, transcripts, IR, Say-Do) are all
        # bottoms-up equity machinery that means nothing for a fund.
        instrument_value = getattr(instrument, "value", instrument)
        if str(instrument_value or "").lower() == "etf":
            try:
                run_id = start_run(
                    conn,
                    directive="onboard_ticker",
                    ticker_scope=[ticker],
                    invocation_inputs=_onboard_invocation_inputs(
                        args,
                        ticker,
                        instrument=str(instrument_value or "").lower() or None,
                    ),
                )
            except PipelineRunSuppressedError as exc:
                print(json.dumps(suppression_payload(exc)))
                return 0
            rc = run_etf_onboarding(conn, ticker, PROJECT_ROOT)
            end_run(
                conn,
                run_id,
                RunStageStatus.OK if rc == 0 else RunStageStatus.FAILED,
                error_summary=None if rc == 0 else f"etf onboarding rc={rc}",
            )
            elapsed = (datetime.now() - started).total_seconds()
            print(f"[onboard] {ticker} done in {elapsed:.1f}s (etf path rc={rc})", flush=True)
            return rc

        print(f"[onboard] {ticker} stage=quarterly_refresh", flush=True)
        transcript_artifacts = stage_pending_issuer_transcripts(
            conn,
            tickers=[ticker],
            project_root=PROJECT_ROOT,
            private_root=PROJECT_ROOT / ".tmp" / "transcript-acquisition",
            entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
            as_of=date.today(),
        )
        for artifact in transcript_artifacts.values():
            persist_authorized_transcript_artifact(
                conn,
                artifact,
                project_root=PROJECT_ROOT,
            )
        conn.commit()

        def _revalidate_transcript_batch() -> None:
            for artifact in transcript_artifacts.values():
                read_authorized_transcript(conn, artifact, project_root=PROJECT_ROOT)

        try:
            run_id = start_run(
                conn,
                directive="onboard_ticker",
                ticker_scope=[ticker],
                invocation_inputs=_onboard_invocation_inputs(
                    args,
                    ticker,
                    instrument=str(instrument_value or "").lower() or None,
                ),
                pre_persist_validation=_revalidate_transcript_batch,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0
        report = refresh_ticker(
            conn,
            ticker=ticker,
            project_root=PROJECT_ROOT,
            holdings_dir=_HOLDINGS_DIR,
            run_id=run_id,
            fetch_sec=False,
            transcript_artifacts=transcript_artifacts,
        )
        any_failed = any(s.status is RefreshStageStatus.FAILED for s in report.stages)
        end_run(
            conn,
            run_id,
            RunStageStatus.OK if not any_failed else RunStageStatus.FAILED,
            error_summary="one or more stages failed" if any_failed else None,
        )
        for s in report.stages:
            print(
                f"[onboard] {ticker} {s.name.value:24s} {s.status.value:8s} rows={s.rows_processed:<5} {s.notes}",
                flush=True,
            )
    finally:
        conn.close()

    transcript_rc: int | None = None
    if not args.skip_transcripts:
        print(f"[onboard] {ticker} stage=backfill_transcripts", flush=True)
        transcript_rc = _run_transcript_backfill(ticker)
        if transcript_rc != 0:
            # Aggregator gaps are expected; log but don't fail the onboard.
            print(
                f"[onboard] {ticker} backfill_transcripts returned rc={transcript_rc}; "
                f"continuing (aggregator misses are tolerated)",
                flush=True,
            )

    ir_rc: int | None = None
    if not args.skip_ir:
        print(f"[onboard] {ticker} stage=ir_documents", flush=True)
        ir_rc = _run_ir_documents(ticker)
        if ir_rc != 0:
            # IR sites vary wildly / may not be discoverable; best-effort, the
            # weekly cron retries. Never fail the onboard on this.
            print(
                f"[onboard] {ticker} ir_documents returned rc={ir_rc}; "
                f"continuing (IR discovery is best-effort)",
                flush=True,
            )

    # Say-Do: per-quarter transcript summaries + pairwise §6 cards. Gated to
    # evaluation-list names (per onboarding policy) unless --force-saydo. Runs
    # after transcripts are ingested + registered so --regenerate-missing finds
    # them. Best-effort — LLM-bound and tolerant of coverage gaps.
    if not args.skip_saydo:
        list_type = _lookup_list_type(ticker)
        if _saydo_should_run(
            list_type,
            force=args.force_saydo,
            instrument=str(instrument_value or "").lower() or None,
        ):
            print(f"[onboard] {ticker} stage=saydo (list_type={list_type})", flush=True)
            saydo_rc = _run_saydo(ticker)
            if saydo_rc != 0:
                print(
                    f"[onboard] {ticker} saydo returned rc={saydo_rc}; "
                    f"continuing (Say-Do generation is best-effort)",
                    flush=True,
                )
        else:
            print(
                f"[onboard] {ticker} stage=saydo SKIPPED "
                f"(list_type={list_type}, not evaluation; use --force-saydo to override)",
                flush=True,
            )

    elapsed = (datetime.now() - started).total_seconds()
    print(
        f"[onboard] {ticker} done in {elapsed:.1f}s; "
        f"failed_stages={any_failed}; transcript_rc={transcript_rc}",
        flush=True,
    )
    return 0 if not any_failed else 1


if __name__ == "__main__":
    sys.exit(main())

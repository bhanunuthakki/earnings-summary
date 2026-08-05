"""Orchestrate the morning pipeline: news -> triggers -> feed -> validate.

The daily trigger driver (``run_triggers.py``) fires alerts and persists them;
the feed renderer (``build_alert_feed.py``) renders HTML from those persisted
alerts. On their own they are unchained: the driver runs at 04:00 via cron,
but the feed HTML is only rebuilt when manually invoked, so a 07:00 read shows
stale HTML. This orchestrator chains the stages into one scheduled run. (The
morning-digest render stage retired with the standalone /digest page,
2026-06-11 — the live Home rail serves that view straight from the DB.)

Six stages run in sequence as subprocess-isolated children:

  0. news     -- ``fetch_news.py`` (ingest fresh per-ticker news into the
     ``news`` table so the material_news trigger has stories to classify;
     ``--news-source`` selects FMP / WebSearch+Opus / auto).
  0a. list_type -- ``sync_list_type_from_holdings.py --apply`` (sync
     tracked_companies.list_type to the tracker's holdings: held > $100 operating
     company => portfolio, unheld portfolio name => evaluation; runs before every
     downstream stage that reads the portfolio set; safe no-op on empty/absent
     holdings so a tracker outage never demotes the book).
  0b. decisions -- ``record_decisions.py`` (record memo verdicts into the
     ``decisions`` ledger + extract falsifiable "what would change my mind"
     conditions, so the decision_condition trigger evaluates fresh rows).
  0c. lifecycle -- ``sync_position_lifecycle.py`` (reconcile the
     position_entries entry/exit ledger against the portfolio list + tracker
     holdings, snapshotting the fresh stage-0b conditions at entry).
  1. triggers -- ``run_triggers.py`` (the long pole; fans LLM-backed sensors
     across the portfolio, cost-capped via ``--max-cost-usd``).
  2. feed     -- ``build_alert_feed.py``.
  3. validate -- ``run_validation_engine.py --gate`` (LAST so it never blocks a
     render): runs the population-level data checks and makes a HALT-severity
     result a failed stage, so egregious data lands in the pipeline exit code
     for monitoring. The standing machinery that *runs* the validation gate.

Resilience contract (the load-bearing behavior):

  * The orchestrator NEVER aborts early. A failed or timed-out stage is logged
    and the remaining stages still run. The feed is a read-only render over
    whatever alerts already exist, so a trigger failure must not leave the
    user staring at a stale feed -- the render runs regardless. Likewise a
    failed news fetch (stage 0) never blocks the trigger sweep: triggers run
    over whatever news already exists, degrading to none.
  * Each stage's stdout/stderr is captured and echoed under a stage header.
  * Exit code is the count of failed stages (0 = all good), reported only AFTER
    every non-skipped stage has been attempted. This lets cron / monitoring
    detect partial failure while still producing the best-effort feed.

Usage:
    python execution/run_morning_pipeline.py
    python execution/run_morning_pipeline.py --news-source websearch
    python execution/run_morning_pipeline.py --max-cost-usd 10 --skip-news
    python execution/run_morning_pipeline.py --user-id bhanu --db-path /tmp/x.db
    python execution/run_morning_pipeline.py --skip-triggers   # re-render only

``--skip-triggers`` runs the feed render only -- useful for re-rendering after
manual approve/dismiss actions mutate the alert rows, without paying for
another trigger sweep (it skips stage 0 news too). ``--skip-news`` skips only
the news fetch.

This orchestrates the scripts via subprocess (process isolation, matching
the repo's drain-executor + daily-fetch-and-brief pattern) rather than importing
their ``main()``. The child interpreter is ``sys.executable`` -- the exact
interpreter running this orchestrator -- so the children never depend on PATH
resolution differing from the parent's.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
# The repo root too, so the post-flight dead-man check can import
# ``execution.verify_daily_chain`` (a namespace package — ``execution/`` has no
# ``__init__.py``). Without it that import raises ModuleNotFoundError into a
# swallowing ``except``, and the artifact every external monitor keys off is
# silently never written.
sys.path.insert(0, str(PROJECT_ROOT))

from llm import tracectx  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    suppression_payload,
)
from run_lock import RunLockHeldError, acquire_run_lock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DEFAULT_USER_ID = os.environ.get("CIO_USER_ID", "bhanu")
DEFAULT_MAX_COST_USD = 10.0

# AGENTS.md concurrency rule: one process owns the portfolio.db write set at a
# time; scheduled and interactive runs honor the SAME run lock. The
# orchestrator holds it for the whole run so a db_gc apply (which acquires the
# same lock, execution/db_gc.py) can never interleave its bulk deletes with
# the morning write legs — the 2026-07-31 lock-starvation incident class. A
# held lock is waited on briefly (a healthy db_gc batch run yields within its
# own budget), then fails the run loudly rather than writing into contention.
_RUN_LOCK_WAIT_S = 900.0

# Per-stage wall-clock caps. Stage 1 fans LLM-backed sensors across the whole
# portfolio and is the long pole -- 30 min mirrors the slice-1 guidance. The
# feed render stage is a pure SQLite read + HTML write; 5 min is generous.
_TRIGGERS_TIMEOUT_S = 1800
_RENDER_TIMEOUT_S = 300
# Stage 0 (news) is fast on the FMP path, but FMP's stock-news endpoint now
# 402s for the whole book (verified live 2026-07-03), so `auto` falls back to
# an ~55s Opus web-search call per ticker -- fetch_news.py bounds this to
# ceil(tickers/8) concurrent batches x a 60s per-ticker cap (see
# _PRIMARY_WORKERS/_TICKER_TIMEOUT_S there), ~715-780s worst case for a ~98-
# ticker book. The additive EDGAR (sequential, SEC-throttled ~0.15s/ticker) +
# yfinance-grades (threaded) feeds add a low single-digit number of minutes on
# top, hence 1200s (20 min) total -- this stage's honest cost once FMP news is
# gone, not padding.
_NEWS_TIMEOUT_S = 1200
# Stage 3 (data validation) runs population-level range / magnitude-jump /
# source-disagreement checks across every tracked ticker — a SQLite-bound sweep;
# 10 min is generous.
_VALIDATE_TIMEOUT_S = 600
# Stage 0b (decisions) records memo verdicts (regex, cheap) and extracts
# falsifiable conditions for NEW decisions only (one Haiku call each, ≤50 per
# run). Normal mornings touch 0-2 rows; 15 min covers a backfill day.
_DECISIONS_TIMEOUT_S = 900
# Stage 0a (list_type reconcile) syncs tracked_companies.list_type to the
# tracker's actual holdings — pure SQLite (MAIN DB write + tracker RO read).
# Sub-second; 2 min is generous. Runs FIRST so 0c lifecycle / 1 triggers /
# 0f candidate-fit all read the freshly-reconciled portfolio set.
_LIST_TYPE_TIMEOUT_S = 120
# Stage 0c (lifecycle) reconciles position_entries against the portfolio list
# + tracker holdings — pure SQLite + one loopback HTTP group (sub-second
# connect cap when the tracker is down). 2 min is generous.
_LIFECYCLE_TIMEOUT_S = 120
_DECISION_ACTIONS_TIMEOUT_S = 120
# Stage 0d (cockpit fundamentals) runs the financial_facts double-scan that
# was measured at ~1.2s on prod (726k rows). 60s is generous.
_FUNDAMENTALS_TIMEOUT_S = 60
# Stage 0d2 (derived metrics) runs the bottoms-up metrics engine
# (compute_derived_metrics.py --all, portfolio+evaluation scope): pure
# SQLite compute over financial_facts (the idempotent input-fingerprint
# skip makes a no-change morning a fast no-op) PLUS one threaded live-price
# prefetch for the Phase-3 valuation formulas (8 workers x a 15s per-ticker
# cap, the same budget shape as stage 0e's reprice fetch: worst case
# ceil(~48/8) * 15s = 90s for the price leg). First-ever run over a
# ticker's full history is the slow path (~39 formulas x ~20 quarters of
# attempt rows); a steady-state morning rewrites only the valuation rows
# (the live price changes daily, changing their fingerprint) plus any
# quarter whose facts actually changed. No LLM calls -- does not compete
# for the 04:00 window's protected Claude-CLI quota. 10 min covers a cold
# backfill morning; a measured steady-state run is expected well under 2.
_DERIVED_METRICS_TIMEOUT_S = 600
# Stage 0e (DCF re-price) re-divides each persisted fair value by a fresh live
# price (per-ticker source-stack read, no DCF rebuild) so the trim/sell ladder +
# next-dollar "ret" factor don't run on a stale price leg. Reads only
# is_latest=1 rows (~96 tickers, not the ~149-row superseded-history table) and
# fetches threaded (8 workers x a 15s per-ticker cap — src/dcf/reprice.py), so
# the worst case (every ticker hangs its full budget) is
# ceil(96/8) * 15s = 180s; a healthy morning measured ~55s live against prod
# 2026-07-03. 5 min keeps real headroom over both.
_REPRICE_TIMEOUT_S = 300
# Stage 0f (candidate fit, L?) scores each evaluation name's fit to the held book
# off the daily price cache + one tracker analytics fetch. ~30 screen names, a
# covariance-grade read each plus a loopback HTTP group — 3 min is generous and
# covers a tracker that is slow to answer.
_CANDIDATE_FIT_TIMEOUT_S = 180
# Stage 0g (factor proxies) refreshes the 5 ETF style-proxy close series PLUS
# the held-ETF price series the FMP cache doesn't cover (FLKR) from yfinance
# into data/factor_proxies/ so the Risk panel's value/size/momentum loadings
# AND the correlation/Monte-Carlo sections read a fresh local store (never
# the network). ~6 small downloads; 5 min covers a throttled morning. A
# failure keeps the last-good files.
_FACTOR_PROXIES_TIMEOUT_S = 300
# Stage 0h (naked-position gate, Monthly Red Team Phase 1 guard 7) computes
# three read-only checks per held name (downside trigger encoded, realistic
# bear persisted, thesis fresh) and materializes the result to
# data/dashboard/position_guard.json — a handful of read-only SQLite queries
# plus small holdings-JSON file reads, no network, no LLM. 2 min is generous.
_POSITION_GUARD_TIMEOUT_S = 120
# Stage 0i (risk snapshot, PRD §7.1 P0-A) is the AUTHORITATIVE Risk Budget
# writer: one tracker analytics fetch, drawdown + full factor roll-up
# (including the rate leg), RiskBudgetSnapshot validation, then the
# latest-view upsert + content-hash-deduped history append. No LLM. The
# tracker's beta/position-alpha endpoints measured ~22s warm each on prod
# (2026-07-23) and the script's per-endpoint budget is 45s, so 5 min keeps
# honest headroom over a cold sequential fetch.
_RISK_SNAPSHOT_TIMEOUT_S = 300
# Stage 0j (wealth context, PRD §7.6 P0-F) appends one aggregates-only
# household balance-sheet observation per day (tracker live total +
# wealthplan cash/illiquid/home-equity + label-only cash-need band). Pure
# HTTP loopback + sibling-checkout file read, no LLM. 2 min is generous.
_WEALTH_CONTEXT_TIMEOUT_S = 120
# Stage 1b (proactive standup, L9) composes a grounded brief through the Ask
# engine + an eval-judge pass per surviving trip. Rate limits cap it at a few
# deliveries/day, but each is a streamed `claude -p` answer plus ≤2 follow-ups
# plus the judge — 15 min covers a morning where several names trip at once.
_STANDUP_TIMEOUT_S = 900
# Stage 1c (pre-earnings briefs, 2026-07-31 owner ruling): ≤1 Sonnet call per
# in-window name (portfolio + opted-in evaluation names reporting within 7d;
# artifact input-hash cache + T-1 refresh gate bound each name to ≤2 calls per
# earnings cycle). A busy week is ~3-5 calls at ~60-90s each; 10 min is
# generous. Budget 'skip' mode (0260) means a blown cap exits fast and clean.
_PRE_ER_BRIEF_TIMEOUT_S = 600
# Stage 1d (post-earnings readouts): one cached Sonnet-tier synthesis per
# portfolio name's newly selected quarterly transcript. Evaluation names are
# structurally excluded and can spend only through the explicit cockpit action.
_POST_ER_READOUT_TIMEOUT_S = 900

# Canonical stage keys, in run order. Used to build the final summary so a
# skipped stage still appears (as "skipped") even though it never ran.
STAGE_PREFLIGHT = "stage_preflight"
STAGE_NEWS = "stage_0_news"
STAGE_LIST_TYPE = "stage_0a_list_type"
STAGE_DECISIONS = "stage_0b_decisions"
STAGE_LIFECYCLE = "stage_0c_lifecycle"
STAGE_DECISION_ACTIONS = "stage_0c2_decision_actions"
STAGE_FUNDAMENTALS = "stage_0d_fundamentals"
STAGE_DERIVED_METRICS = "stage_0d2_derived_metrics"
STAGE_REPRICE = "stage_0e_reprice"
STAGE_CANDIDATE_FIT = "stage_0f_candidate_fit"
STAGE_FACTOR_PROXIES = "stage_0g_factor_proxies"
STAGE_POSITION_GUARD = "stage_0h_position_guard"
STAGE_RISK_SNAPSHOT = "stage_0i_risk_snapshot"
STAGE_WEALTH_CONTEXT = "stage_0j_wealth_context"
STAGE_TRIGGERS = "stage_1_triggers"
STAGE_STANDUP = "stage_1b_standup"
STAGE_PRE_ER_BRIEF = "stage_1c_pre_earnings_briefs"
STAGE_POST_ER_READOUT = "stage_1d_post_earnings_readouts"
STAGE_FEED = "stage_2_feed"
STAGE_VALIDATE = "stage_3_validate"
_ALL_STAGE_KEYS = (
    STAGE_PREFLIGHT,
    STAGE_NEWS,
    STAGE_LIST_TYPE,
    STAGE_DECISIONS,
    STAGE_LIFECYCLE,
    STAGE_DECISION_ACTIONS,
    STAGE_FUNDAMENTALS,
    STAGE_DERIVED_METRICS,
    STAGE_REPRICE,
    STAGE_CANDIDATE_FIT,
    STAGE_FACTOR_PROXIES,
    STAGE_POSITION_GUARD,
    STAGE_RISK_SNAPSHOT,
    STAGE_WEALTH_CONTEXT,
    STAGE_TRIGGERS,
    STAGE_STANDUP,
    STAGE_PRE_ER_BRIEF,
    STAGE_POST_ER_READOUT,
    STAGE_FEED,
    STAGE_VALIDATE,
)

# Timeout for the environment preflight (should be sub-second).
_PREFLIGHT_TIMEOUT_S = 30

# News ingestion source for Stage 0. Default `auto` (self-healing): FMP first,
# falling back to WebSearch+Opus per ticker on refusal — so the material_news
# trigger keeps eating regardless of FMP's free-tier news policy. (The plan's
# one-shot probe couldn't determine that policy with no FMP key configured.)
DEFAULT_NEWS_SOURCE = "auto"
NEWS_SOURCES = ("fmp", "websearch", "auto")


class StageStatus(StrEnum):
    """Outcome of a single pipeline stage."""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class _Stage:
    """One pipeline stage: a labeled, time-bounded subprocess invocation."""

    key: str
    label: str
    argv: list[str]
    timeout_s: int


@dataclass(slots=True)
class _StageResult:
    """Outcome of running one ``_Stage``.

    ``exit_code`` is None when the child never produced one (timeout or spawn
    failure); ``error`` carries the human-readable reason in that case.
    """

    key: str
    status: StageStatus
    exit_code: int | None
    elapsed_seconds: float
    error: str | None = None


def _build_preflight_stage() -> _Stage:
    """Return the environment preflight stage (validate_environment.py)."""
    return _Stage(
        key=STAGE_PREFLIGHT,
        label="Stage preflight - environment check (validate_environment.py)",
        argv=[sys.executable, str(PROJECT_ROOT / "execution" / "validate_environment.py")],
        timeout_s=_PREFLIGHT_TIMEOUT_S,
    )


def _stage_args_for(args: argparse.Namespace, *, include_max_cost: bool) -> list[str]:
    """Common pass-through args shared by the trigger + feed stages.

    ``--user-id`` and ``--db-path`` go to both scripts; ``--max-cost-usd``
    is meaningful only to the trigger stage (the renderer has no such flag).
    ``--db-path`` is omitted when unset so each script applies its own default
    DB resolution rather than receiving a literal ``None``.
    """
    passthrough: list[str] = ["--user-id", args.user_id]
    if include_max_cost:
        passthrough += ["--max-cost-usd", str(args.max_cost_usd)]
    if args.db_path is not None:
        passthrough += ["--db-path", str(args.db_path)]
    return passthrough


def _build_stages(args: argparse.Namespace) -> list[_Stage]:
    """Construct the ordered stage list from CLI args.

    Stage 1 is omitted entirely when ``--skip-triggers`` is set; the feed
    stage always runs.
    """
    py = sys.executable
    exec_dir = PROJECT_ROOT / "execution"
    stages: list[_Stage] = []

    # Stage 0 -- news fetch, BEFORE triggers so the morning's fresh news is
    # classified in the same run. Skipped by --skip-news, and implicitly by
    # --skip-triggers (the re-render-only path: no point fetching news that won't
    # be classified). The news fetcher takes neither --user-id nor --max-cost-usd,
    # so it does not use _stage_args_for; only --db-path is forwarded (when set).
    if not args.skip_triggers and not args.skip_news:
        db_path_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_NEWS,
                label="Stage 0 - news fetch (fetch_news.py)",
                argv=[
                    py,
                    str(exec_dir / "fetch_news.py"),
                    "--source",
                    args.news_source,
                    *db_path_args,
                ],
                timeout_s=_NEWS_TIMEOUT_S,
            )
        )

    # Stage 0a -- list_type reconcile: sync tracked_companies.list_type to the
    # tracker's actual holdings (held > $100 operating company = portfolio; an
    # unheld portfolio name demotes to evaluation, staying fully briefed) BEFORE
    # every downstream stage that reads the portfolio set (0c lifecycle, 1
    # triggers, 0f candidate-fit). --apply writes; a missing/empty tracker is a
    # safe no-op (the reconciler refuses to demote on zero holdings). Skipped on
    # the re-render-only path. Takes --user-id + --db-path.
    if not args.skip_triggers:
        list_type_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_LIST_TYPE,
                label="Stage 0a - list_type reconcile (sync_list_type_from_holdings.py)",
                argv=[
                    py,
                    str(exec_dir / "sync_list_type_from_holdings.py"),
                    "--apply",
                    "--user-id",
                    args.user_id,
                    *list_type_db_args,
                ],
                timeout_s=_LIST_TYPE_TIMEOUT_S,
            )
        )

    # Stage 0b -- decision ledger + falsifiable-condition extraction, BEFORE
    # triggers so the decision_condition sensor evaluates fresh conditions in
    # the same run. Skipped with triggers (re-render-only path: the sensor
    # won't run, so there is nothing to prepare). Takes --repo-root/--db-path
    # only — no --user-id (the decisions ledger is single-operator).
    if not args.skip_triggers:
        decisions_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_DECISIONS,
                label="Stage 0b - decision conditions (record_decisions.py)",
                argv=[
                    py,
                    str(exec_dir / "record_decisions.py"),
                    *decisions_db_args,
                ],
                timeout_s=_DECISIONS_TIMEOUT_S,
            )
        )
        # Stage 0c -- position-lifecycle reconciler (S5 PR2): opens/closes
        # position_entries on portfolio transitions, snapshotting the fresh
        # stage-0b conditions at entry. Skipped with triggers (same
        # re-render-only rationale). Takes --user-id + --db-path.
        lifecycle_args = ["--user-id", args.user_id, *decisions_db_args]
        stages.append(
            _Stage(
                key=STAGE_LIFECYCLE,
                label="Stage 0c - position lifecycle (sync_position_lifecycle.py)",
                argv=[
                    py,
                    str(exec_dir / "sync_position_lifecycle.py"),
                    *lifecycle_args,
                ],
                timeout_s=_LIFECYCLE_TIMEOUT_S,
            )
        )
        # Stage 0c2 -- decision→action reconciler (Track B): match NULL-action
        # decisions to the tracker's subsequent fills and write user_action_kind,
        # so calibration's action mix stops being structurally empty. Runs after
        # the lifecycle reconciler (both read the tracker's transaction window).
        # Only --db-path is forwarded (the decisions ledger is single-operator).
        # Skipped on the re-render-only path (no new fills to reconcile).
        decision_actions_db_args = (
            ["--db-path", str(args.db_path)] if args.db_path is not None else []
        )
        stages.append(
            _Stage(
                key=STAGE_DECISION_ACTIONS,
                label="Stage 0c2 - decision actions (reconcile_decision_actions.py)",
                argv=[
                    py,
                    str(exec_dir / "reconcile_decision_actions.py"),
                    *decision_actions_db_args,
                ],
                timeout_s=_DECISION_ACTIONS_TIMEOUT_S,
            )
        )
        # Stage 0d -- cockpit fundamentals precompute: materialises per-ticker
        # rev_yoy / fcf_margin to data/cockpit_fundamentals.json so the GET /
        # render reads the cache rather than running a ~1.2s ROW_NUMBER() scan
        # over all ~726k financial_facts rows (S12b profiling finding, PR #535).
        # Only --db-path is forwarded (no --user-id: the computation is not
        # user-scoped). Skipped on the re-render-only path (--skip-triggers):
        # financial_facts does not change during the morning pipeline itself.
        fundamentals_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_FUNDAMENTALS,
                label="Stage 0d - cockpit fundamentals (refresh_cockpit_fundamentals.py)",
                argv=[
                    py,
                    str(exec_dir / "refresh_cockpit_fundamentals.py"),
                    *fundamentals_db_args,
                ],
                timeout_s=_FUNDAMENTALS_TIMEOUT_S,
            )
        )
        # Stage 0d2 -- bottoms-up derived metrics (metrics engine Phases 1-3):
        # compute every registry formula (margins/growth/returns/liquidity/
        # leverage/efficiency/per-share + the live-price-wired valuation set)
        # into kpi_facts + metric_computation_attempts BEFORE the trigger
        # sweep (stage 1) reads kpi_facts -- the "runs as a stage immediately
        # after the statement-ingestion stages, before any thesis/break-rule
        # evaluation" placement docs/design/bottoms_up_metrics_engine.md
        # section 5 specifies (statement ingestion itself runs on its own
        # fetch cron, so within THIS pipeline the constraint is simply
        # "before stage 1"). Grouped with the other local-substrate compute
        # refreshes (0d/0e/0f). Idempotent: unchanged facts skip via
        # input_fingerprint, so a normal morning re-writes only the
        # valuation rows. Only --db is forwarded (note: this CLI's flag is
        # --db, not --db-path -- it predates the pipeline wiring and follows
        # derive_kpis_from_fmp.py's flag name); not user-scoped, no LLM.
        # Skipped on the re-render-only path (--skip-triggers).
        derived_metrics_db_args = ["--db", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_DERIVED_METRICS,
                label="Stage 0d2 - derived metrics (compute_derived_metrics.py)",
                argv=[
                    py,
                    str(exec_dir / "compute_derived_metrics.py"),
                    "--all",
                    *derived_metrics_db_args,
                ],
                timeout_s=_DERIVED_METRICS_TIMEOUT_S,
            )
        )
        # Stage 0e -- DCF price-leg re-price (L6): re-divide each persisted fair
        # value by a fresh live price so dcf_runs.over_under_pct + live_price are
        # current BEFORE the trigger sweep reads them. refresh_dcf is opt-in with
        # no cron, so without this the trim/sell ladder + 50%-weight next-dollar
        # "ret" factor run on a price leg frozen at the last rebuild. Pure
        # arithmetic over the stored fair value — no DCF rebuild. Only --db-path
        # is forwarded (not user-scoped). Skipped on the re-render-only path.
        reprice_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_REPRICE,
                label="Stage 0e - DCF re-price (reprice_dcf.py)",
                argv=[
                    py,
                    str(exec_dir / "reprice_dcf.py"),
                    *reprice_db_args,
                ],
                timeout_s=_REPRICE_TIMEOUT_S,
            )
        )
        # Stage 0f -- candidate fit (the cockpit Fit column): scores each
        # evaluation name's fit to the held book (Marginal Sharpe · diversification
        # · factor exposure · sector) and materialises data/candidate_fit.json so
        # the GET / render reads the cache rather than running covariance-grade
        # price reads per candidate. Runs after the weights cache (0c) and DCF
        # re-price (0e) so the held weights + DCF reward it reads are fresh. Only
        # --db-path is forwarded (the book read is single-user; the tracker URL
        # comes from the env). Skipped on the re-render-only path (--skip-triggers).
        candidate_fit_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_CANDIDATE_FIT,
                label="Stage 0f - candidate fit (refresh_candidate_fit.py)",
                argv=[
                    py,
                    str(exec_dir / "refresh_candidate_fit.py"),
                    *candidate_fit_db_args,
                ],
                timeout_s=_CANDIDATE_FIT_TIMEOUT_S,
            )
        )
        # Stage 0g -- factor proxies: refresh the ETF style-proxy close series
        # (SPY/VTV/VUG/IWM/MTUM) PLUS held-ETF price series the FMP cache
        # doesn't cover (FLKR) from yfinance into data/factor_proxies/ so the
        # Risk panel's value/size/momentum loadings AND the correlation/
        # Monte-Carlo sections read a fresh LOCAL store — the render path
        # never touches the network. Not user-scoped, no LLM; --repo-root
        # follows the db override so tests/dev runs never write the real
        # repo's data/. Skipped on the re-render-only path.
        proxies_root_args = (
            ["--repo-root", str(args.db_path.parent.parent)] if args.db_path is not None else []
        )
        stages.append(
            _Stage(
                key=STAGE_FACTOR_PROXIES,
                label="Stage 0g - factor proxies (fetch_factor_proxies.py)",
                argv=[
                    py,
                    str(exec_dir / "fetch_factor_proxies.py"),
                    *proxies_root_args,
                ],
                timeout_s=_FACTOR_PROXIES_TIMEOUT_S,
            )
        )
        # Stage 0h -- naked-position gate (Monthly Red Team Phase 1 guard 7):
        # materializes data/dashboard/position_guard.json so the Risk panel's
        # render path reads the cache instead of recomputing the three
        # per-name checks (downside trigger / realistic bear / thesis
        # freshness) on every request. Runs after 0c lifecycle (fresh
        # materialized weights) and 0g factor proxies (no ordering dependency
        # on it, just grouped with the other local-substrate cache refreshes).
        # Only --db-path is forwarded (not user-scoped, no LLM). Skipped on
        # the re-render-only path (--skip-triggers).
        position_guard_db_args = (
            ["--db-path", str(args.db_path)] if args.db_path is not None else []
        )
        stages.append(
            _Stage(
                key=STAGE_POSITION_GUARD,
                label="Stage 0h - naked-position gate (refresh_position_guard.py)",
                argv=[
                    py,
                    str(exec_dir / "refresh_position_guard.py"),
                    *position_guard_db_args,
                ],
                timeout_s=_POSITION_GUARD_TIMEOUT_S,
            )
        )
        # Stage 0i -- risk snapshot (PRD §7.1 P0-A): the authoritative Risk
        # Budget writer. Runs after 0g factor proxies so the factor roll-up's
        # local stores are fresh; validation failures write nothing and fire
        # the data_feed_stale dead-man (the render path keeps serving the
        # last valid snapshot with its age). Only --db-path is forwarded
        # (single-user; tracker URL comes from the env). Skipped on the
        # re-render-only path (--skip-triggers).
        risk_snapshot_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_RISK_SNAPSHOT,
                label="Stage 0i - risk snapshot (refresh_portfolio_risk_snapshot.py)",
                argv=[
                    py,
                    str(exec_dir / "refresh_portfolio_risk_snapshot.py"),
                    *risk_snapshot_db_args,
                ],
                timeout_s=_RISK_SNAPSHOT_TIMEOUT_S,
            )
        )
        # Stage 0j -- wealth context (PRD §7.6 P0-F): one aggregates-only
        # balance-sheet observation per day (idempotent re-runs dedupe on the
        # content hash). One source down degrades with a warning; both down
        # writes nothing and fires the dead-man. Skipped on the
        # re-render-only path (--skip-triggers).
        wealth_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_WEALTH_CONTEXT,
                label="Stage 0j - wealth context (refresh_wealth_context_snapshot.py)",
                argv=[
                    py,
                    str(exec_dir / "refresh_wealth_context_snapshot.py"),
                    *wealth_db_args,
                ],
                timeout_s=_WEALTH_CONTEXT_TIMEOUT_S,
            )
        )

    if not args.skip_triggers:
        stages.append(
            _Stage(
                key=STAGE_TRIGGERS,
                label="Stage 1 - triggers (run_triggers.py)",
                argv=[
                    py,
                    str(exec_dir / "run_triggers.py"),
                    *_stage_args_for(args, include_max_cost=True),
                ],
                timeout_s=_TRIGGERS_TIMEOUT_S,
            )
        )

        # Stage 1b -- proactive analyst standup (L9), AFTER triggers so the
        # decision-condition sensor's fresh state is in the DB. Watches the four
        # open loops (falsifiable conditions, stale journal items, DCF staleness,
        # position drift) and composes a grounded, eval-gated, rate-limited brief
        # into the persistent standup thread. Paid (Ask engine + eval judge), so
        # it sits on the trigger path and is skippable on its own. Forwards
        # --user-id + --db-path; it owns its rate-limit / eval-bar defaults.
        if not args.skip_standup:
            standup_args: list[str] = ["--user-id", args.user_id]
            if args.db_path is not None:
                standup_args += ["--db-path", str(args.db_path)]
            stages.append(
                _Stage(
                    key=STAGE_STANDUP,
                    label="Stage 1b - proactive standup (run_standup.py)",
                    argv=[py, str(exec_dir / "run_standup.py"), *standup_args],
                    timeout_s=_STANDUP_TIMEOUT_S,
                )
            )

    # Stage 1c -- pre-earnings briefs (owner ruling 2026-07-31: the one
    # artifact allowed to pre-generate). Runs AFTER triggers/standup so the
    # brief's tone + KPI context reflects this morning's classification.
    # Idempotent per (ticker, ER date) via the llm_artifacts input hash; the
    # generator applies the per-item degrade pattern internally and its
    # budget is 'skip' mode, so a lean-quota morning costs zero calls.
    # Takes --db-path only (single-operator scope, cost governed by the
    # purpose budget rather than --max-cost-usd). Skipped with triggers
    # (re-render-only path) and by --skip-pre-earnings-briefs.
    if not args.skip_triggers and not args.skip_pre_earnings_briefs:
        brief_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_PRE_ER_BRIEF,
                label="Stage 1c - pre-earnings briefs (generate_pre_earnings_briefs.py)",
                argv=[
                    py,
                    str(exec_dir / "generate_pre_earnings_briefs.py"),
                    *brief_db_args,
                ],
                timeout_s=_PRE_ER_BRIEF_TIMEOUT_S,
            )
        )

    # Stage 1d -- persisted post-earnings readouts. The generator itself
    # selects only active portfolio names and keys each artifact to the
    # selected transcript's period_end. Repeated mornings are cache hits;
    # evaluation names never enter this scheduled process.
    if not args.skip_triggers and not args.skip_post_earnings_readouts:
        readout_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_POST_ER_READOUT,
                label="Stage 1d - post-earnings readouts (generate_post_earnings_readouts.py)",
                argv=[
                    py,
                    str(exec_dir / "generate_post_earnings_readouts.py"),
                    *readout_db_args,
                ],
                timeout_s=_POST_ER_READOUT_TIMEOUT_S,
            )
        )

    stages.append(
        _Stage(
            key=STAGE_FEED,
            label="Stage 2 - alert feed (build_alert_feed.py)",
            argv=[
                py,
                str(exec_dir / "build_alert_feed.py"),
                *_stage_args_for(args, include_max_cost=False),
            ],
            timeout_s=_RENDER_TIMEOUT_S,
        )
    )

    # Stage 3 -- data validation GATE, LAST so a HALT verdict never blocks the
    # render. ``run_validation_engine.py --gate`` runs the population-level
    # range / magnitude-jump / source-disagreement checks and exits non-zero
    # when this run records a HALT-severity issue (a 10x unit error, a >1000x
    # range violation). ``_run_stage`` turns that non-zero exit into a FAILED
    # stage, so egregious data surfaces in the pipeline exit code (which cron /
    # monitoring already watches) WITHOUT aborting the run or mutating anything
    # beyond the engine's own audit rows. This is the standing machinery that
    # *runs* the gate -- the validation engine was otherwise never invoked
    # outside its CLI / tests (v6 re-grade, Quality enforcement). The engine
    # takes neither --user-id nor --max-cost-usd, so its argv is built directly
    # (only --db-path is forwarded, when set). Skipped on the re-render-only path
    # (--skip-triggers: fact data did not change) and by --skip-validation.
    if not args.skip_triggers and not args.skip_validation:
        validate_db_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_VALIDATE,
                label="Stage 3 - data validation gate (run_validation_engine.py --gate)",
                argv=[
                    py,
                    str(exec_dir / "run_validation_engine.py"),
                    "--gate",
                    *validate_db_args,
                ],
                timeout_s=_VALIDATE_TIMEOUT_S,
            )
        )
    return stages


def _echo_captured_output(stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    """Write a subprocess's captured stdout/stderr to the parent's own streams.

    Shared by the normal-exit and timeout paths so a killed stage's partial
    output (whatever it flushed before the kill) is echoed exactly like a
    completed stage's would be -- see ``_run_stage``'s docstring. The stages
    run with ``text=True`` so both are ``str`` in practice; ``TimeoutExpired``
    types its attributes ``bytes | None``, so bytes are decoded defensively."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)


def _run_stage(stage: _Stage) -> _StageResult:
    """Invoke one stage as a subprocess; echo its output under a header.

    Never raises. ``subprocess.run`` is called with ``check=False`` so a
    non-zero child exit returns a CompletedProcess rather than raising; the
    only runtime exceptions left are ``TimeoutExpired`` and ``OSError``
    (spawn failure), both caught and turned into a failed ``_StageResult``.
    This is what guarantees the caller's loop never aborts early.

    ``capture_output=True`` buffers the child's stdout/stderr entirely in
    memory and only returns it on a *normal* exit. A killed-on-timeout child's
    ``TimeoutExpired`` exception carries whatever was captured before the kill
    on its own ``.stdout`` / ``.stderr`` attributes (Python drains the pipes
    before raising) -- this is echoed on a timeout too, so a future hang shows
    the last progress line(s) the child managed to flush (e.g. reprice's
    per-ticker ``reprice_ticker_done`` events, or fetch_news's per-ticker
    events) instead of a completely empty stage section in the cron log.
    """
    sys.stdout.write(f"\n{'=' * 72}\n=== {stage.label}\n{'=' * 72}\n")
    sys.stdout.flush()

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            stage.argv,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=stage.timeout_s,
            check=False,
            # P1 trace context (llm.tracectx): stages are SUBPROCESSES, so an
            # in-process contextvar cannot reach them — the trace is propagated
            # through the environment (the same mechanism OTel uses across
            # process boundaries). Every llm_calls row the child writes is then
            # attributable to this stage, turning "which stage burns the
            # morning's tokens?" from ~15 hand-written queries into one GROUP BY.
            env=tracectx.child_env(stage_name=f"morning_pipeline.{stage.key}"),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - t0, 3)
        _echo_captured_output(exc.stdout, exc.stderr)
        return _fail(stage, f"timed out after {stage.timeout_s}s", elapsed)
    except OSError as exc:
        elapsed = round(time.monotonic() - t0, 3)
        return _fail(stage, f"spawn failed: {exc}", elapsed)

    elapsed = round(time.monotonic() - t0, 3)
    _echo_captured_output(proc.stdout, proc.stderr)

    if proc.returncode == 0:
        sys.stdout.write(f"[{stage.key}] OK (exit 0, {elapsed}s)\n")
        return _StageResult(
            key=stage.key,
            status=StageStatus.OK,
            exit_code=0,
            elapsed_seconds=elapsed,
        )
    return _fail(
        stage,
        f"exited {proc.returncode}",
        elapsed,
        exit_code=proc.returncode,
    )


def _fail(
    stage: _Stage,
    reason: str,
    elapsed: float,
    *,
    exit_code: int | None = None,
) -> _StageResult:
    """Emit a prominent failure banner (stderr) and a status line (stdout).

    Stage 1's failure is deliberately tolerated -- the feed still renders --
    so it must be loud in the cron log rather than buried in the child's
    captured output.
    """
    sys.stderr.write(f"\n!!! [{stage.key}] FAILED - {reason}\n")
    sys.stdout.write(f"[{stage.key}] FAILED - {reason} ({elapsed}s)\n")
    return _StageResult(
        key=stage.key,
        status=StageStatus.FAILED,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        error=reason,
    )


def _summarize(results: list[_StageResult], *, elapsed_seconds: float) -> dict[str, object]:
    """Build the final summary dict keyed by the canonical stage keys.

    A stage that never ran (via the ``--skip-*`` flags) is reported as
    "skipped" so the summary always carries every stage key.
    """
    by_key = {r.key: r.status.value for r in results}
    summary: dict[str, object] = {
        key: by_key.get(key, StageStatus.SKIPPED.value) for key in _ALL_STAGE_KEYS
    }
    summary["elapsed_seconds"] = elapsed_seconds
    return summary


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help=f"Per-run LLM cost cap (USD) passed to the trigger stage "
        f"(default {DEFAULT_MAX_COST_USD}). Ignored by the render stages.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help=f"Owner of the alerts / feed rows. Default: "
        f"{DEFAULT_USER_ID!r}. Passed through to every stage.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path for every stage. When unset, each "
        "script applies its own default (data/portfolio.db under the repo root).",
    )
    parser.add_argument(
        "--skip-triggers",
        action="store_true",
        help="Skip stage 1 (triggers) and run only the feed render. "
        "Useful for re-rendering after manual approve/dismiss actions. Also "
        "skips stage 0 (news), since there is nothing to classify.",
    )
    parser.add_argument(
        "--news-source",
        choices=NEWS_SOURCES,
        default=DEFAULT_NEWS_SOURCE,
        help=f"Stage 0 news source (default: {DEFAULT_NEWS_SOURCE!r}). 'auto' runs "
        f"FMP and falls back to WebSearch+Opus per ticker on refusal; 'websearch' "
        f"once FMP's news is cut off; 'fmp' to disable the LLM fallback.",
    )
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="Skip stage 0 (news fetch) only — triggers still run over whatever "
        "news rows already exist.",
    )
    parser.add_argument(
        "--skip-standup",
        action="store_true",
        help="Skip stage 1b (the proactive analyst standup). The standup composes "
        "an eval-gated, rate-limited advisory brief per surviving trip through the "
        "Ask engine — skip it to run the trigger sweep without the paid standup leg.",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip stage 4 (the data-validation gate). The gate runs the "
        "validation engine and makes a HALT-severity result a failed stage "
        "(non-zero pipeline exit) so monitoring catches egregious data; skip it "
        "to run the pipeline without the data check.",
    )
    parser.add_argument(
        "--skip-pre-earnings-briefs",
        action="store_true",
        help="Skip stage 1c (pre-earnings brief generation). The stage is "
        "already a no-op outside each name's 7-day pre-ER window and is "
        "idempotent inside it; skip it to run a brief-free pipeline.",
    )
    parser.add_argument(
        "--skip-post-earnings-readouts",
        action="store_true",
        help="Skip stage 1d (portfolio-only persisted post-earnings readouts).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Supersede an already-running morning pipeline attempt.",
    )
    return parser.parse_args(argv)


def _record_run(
    db_path: Path,
    *,
    start: bool,
    run_id: str | None = None,
    failed: bool = False,
    error_summary: str | None = None,
    invocation_inputs: dict[str, str | float | bool] | None = None,
    force: bool = False,
) -> str | None:
    """Wrap start_run / end_run so run accounting failures never crash the pipeline."""
    try:
        from models.runs import StageStatus as RunStatus
        from pipeline.run_accounting import end_run, start_run

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            if start:
                return start_run(
                    conn,
                    directive="run_morning_pipeline",
                    ticker_scope=[],
                    invocation_inputs=invocation_inputs,
                    force=force,
                    deduplicate_completed=True,
                )
            if run_id is not None:
                status = RunStatus.FAILED if failed else RunStatus.OK
                end_run(conn, run_id, status, error_summary)
        finally:
            conn.close()
    except PipelineRunSuppressedError:
        raise
    except Exception as exc:
        sys.stderr.write(f"WARNING: run_accounting failed: {exc}\n")
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    t0 = time.monotonic()

    # Resolve DB path early — needed for run accounting.
    db_path = args.db_path if args.db_path is not None else PROJECT_ROOT / "data" / "portfolio.db"

    # Own the portfolio write set for the whole run (see _RUN_LOCK_WAIT_S).
    # Only taken when the DB exists — test invocations without a database have
    # no write set to guard, and the lock file sits next to the DB so every
    # checkout pointing at the same portfolio.db contends on one lock.
    lock = None
    if db_path.exists():
        try:
            lock = acquire_run_lock(
                db_path,
                owner="run_morning_pipeline",
                timeout_s=_RUN_LOCK_WAIT_S,
                poll_s=5.0,
            )
        except RunLockHeldError as exc:
            sys.stderr.write(f"\n!!! [run_lock] FAILED - {exc}\n")
            return 1
    try:
        return _run_pipeline(args, db_path=db_path, t0=t0)
    finally:
        if lock is not None:
            lock.release()


def _run_pipeline(args: argparse.Namespace, *, db_path: Path, t0: float) -> int:
    """The pipeline body proper, run while holding the portfolio run lock."""
    # Record the pipeline start in ingestion_runs so the dead-man post-flight
    # (verify_daily_chain.py) can confirm it ran today.
    run_id: str | None = None
    if db_path.exists():
        try:
            run_id = _record_run(
                db_path,
                start=True,
                invocation_inputs={
                    "max_cost_usd": args.max_cost_usd,
                    "news_source": args.news_source,
                    "run_date": date.today().isoformat(),
                    "skip_news": args.skip_news,
                    "skip_standup": args.skip_standup,
                    "skip_triggers": args.skip_triggers,
                    "skip_validation": args.skip_validation,
                    "user_id": args.user_id,
                },
                force=args.force,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0

    # Stage preflight: run validate_environment.py before the main stages.
    # A failed preflight is loud (stderr banner + summary entry) but does NOT
    # abort the pipeline — the resilience contract holds for all stages.
    preflight = _run_stage(_build_preflight_stage())

    stages = _build_stages(args)

    # Always-attempt contract: iterate every built stage with no early return.
    # _run_stage never raises, so a failure / timeout in one stage cannot stop
    # the next from running.
    main_results: list[_StageResult] = [_run_stage(stage) for stage in stages]
    results: list[_StageResult] = [preflight, *main_results]

    summary = _summarize(results, elapsed_seconds=round(time.monotonic() - t0, 3))
    sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")

    failed_count = sum(1 for r in results if r.status is StageStatus.FAILED)

    # Close the ingestion_runs row so the dead-man sees a terminal status today.
    if run_id is not None and db_path.exists():
        failed_names = [r.key for r in results if r.status is StageStatus.FAILED]
        err = f"failed stages: {', '.join(failed_names)}" if failed_names else None
        _record_run(
            db_path, start=False, run_id=run_id, failed=bool(failed_count), error_summary=err
        )

    # Post-flight dead-man check: write .tmp/daily_chain_status.json with
    # today's pipeline verdict so the cron_health panel and external monitors
    # can confirm the chain ran (even when the pipeline had partial failures).
    # Runs AFTER end_run so ingestion_runs reflects the terminal status.
    try:
        from execution.verify_daily_chain import main as _vdc_main

        _vdc_main(["--quiet", "--db-path", str(db_path)])
    except Exception as exc:
        # Deliberately non-fatal: a monitoring artifact must not fail the run
        # that produced good data. But it gets the same "!!!" marker a failed
        # stage does — the previous bare WARNING let a permanently broken
        # import (and a never-written artifact) read as routine log noise.
        sys.stderr.write(
            f"\n!!! [post_flight_verify_daily_chain] FAILED - {type(exc).__name__}: {exc}\n"
        )

    # Exit code = number of failed stages (skipped stages are not failures and
    # were never added to `results`). Reported only after all stages ran.
    return failed_count


if __name__ == "__main__":
    sys.exit(main())

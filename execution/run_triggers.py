"""Daily trigger driver — runs registered sensors across the user's portfolio.

Iterates the cartesian product of (ticker, trigger) under
``ENABLED_TRIGGERS`` from ``triggers.registry``. For each pair:

  1. ``scan(ticker, db)`` produces zero or more candidates.
  2. For each candidate, composes the user-state snapshot, gates on
     ``should_fire``, dedupes against ``alerts.signature_sha`` via
     ``find_by_signature``, then runs ``build_alert`` + ``draft_actions``
     and persists both via ``alerts.store`` CRUD.
  3. Per-ticker exceptions are caught and logged so one stuck ticker
     never breaks the fan-out.
  4. A per-run accumulated-cost cap (default $10 via ``--max-cost-usd``)
     halts the loop cleanly when reached — the driver exits 0 with a
     summary noting the cap, not 1.

Usage:
    python execution/run_triggers.py
    python execution/run_triggers.py --tickers META,GOOG --triggers earnings_tone
    python execution/run_triggers.py --dry-run --max-cost-usd 5
    python execution/run_triggers.py --user-id bhanu --db-path /tmp/test.db

Stub triggers (``build_alert`` raises NotImplementedError) are tolerated:
the driver logs ``trigger_stub_skipping`` and moves on. This lets a
half-implemented sensor sit in ``ALL_TRIGGERS`` (via ``--triggers``)
without crashing a scheduled run.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sqlite3
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypeAlias

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from alerts.store import (  # noqa: E402
    compute_signature_sha,
    find_by_signature,
    fire_alert,
    queue_action,
)
from db_paths import resolve_db_path as canonical_db_path  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from triggers.base import (  # noqa: E402
    AlertDraft,
    StatefulTrigger,
    Trigger,
    TriggerCandidate,
    UserStateContext,
)
from triggers.registry import ALL_TRIGGERS, ENABLED_TRIGGERS  # noqa: E402
from user_state.registry import list_kpis  # noqa: E402
from user_state.sizing import list_intents  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_MAX_COST_USD = 10.0
DISMISSED_LOOKBACK_DAYS = 90
RegisteredTrigger: TypeAlias = type[Trigger] | type[StatefulTrigger]


def _emit(event: dict[str, object]) -> None:
    """One structured-log line to stderr (stdout is reserved for the run summary)."""
    sys.stderr.write(json.dumps(event, default=str) + "\n")


# --------------------------------------------------------------------------
# Ticker loading
# --------------------------------------------------------------------------


def resolve_db_path(override: Path | None) -> Path:
    """Resolve the portfolio DB path, preferring an explicit override.

    Falls back to ``db.DB_PATH`` (which lives at
    ``data/portfolio.db`` under the project root). Raises if neither
    resolves to an existing file — the driver is a primary-write path
    and a missing DB is a hard error, not a silent skip.

    When an explicit override is given, it is also synced into ``db.DB_PATH``
    via ``db.set_db_path`` so writers that resolve their DB from the global
    (above all the LLM call ledger) write to the SAME DB as the alert/user_state
    stores — otherwise an LLM-backed trigger's cost rows land in the default DB.
    """
    import db

    path = canonical_db_path(override)
    if path is None or not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    if override is not None:
        db.set_db_path(path)
    return path


def _active_list_types_sql() -> str:
    """SQL ``IN (...)`` fragment naming the lists worth scanning.

    Read from ``db.ACTIVE_LIST_TYPES_SQL`` when the production module is
    importable; otherwise falls back to the canonical literal so the driver
    still runs against a stripped-down test DB (the tests don't depend on
    ``db.py``'s module-level side effects).
    """
    try:
        from db import ACTIVE_LIST_TYPES_SQL
    except ImportError:
        return "('portfolio', 'watchlist', 'evaluation')"
    return ACTIVE_LIST_TYPES_SQL


def _load_portfolio_tickers(db_path: Path) -> list[str]:
    """Tickers in the user's portfolio / watchlist / evaluation lists.

    Mirrors ``daily_fetch_and_brief._resolve_tickers`` with
    ``--all-tracked`` — the trigger driver doesn't care whether
    ``brief_dirty`` is set, only that the ticker is part of the active
    universe. Returns an empty list when the table is absent (fresh
    repo, no migrations applied), which the driver tolerates as
    "nothing to do".
    """
    list_types_sql = _active_list_types_sql()
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    try:
        try:
            rows = conn.execute(
                "SELECT ticker FROM tracked_companies "
                f"WHERE list_type IN {list_types_sql} "
                "AND (archived_at IS NULL) "
                "ORDER BY ticker"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [str(r[0]).upper() for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# User-state snapshot
# --------------------------------------------------------------------------


def _load_dismissed_signatures(db_path: Path, *, user_id: str, lookback_days: int) -> set[str]:
    """Set of ``signature_sha`` values the user has dismissed recently.

    Used to seed ``UserStateContext.recent_dismissed_signatures`` so
    sensors that opt into that gate (future kinds) can refuse to re-fire
    a dismissed alert. The current ``earnings_tone`` sensor does not
    consult this set — ``find_by_signature`` already covers dismissed
    rows since ``status != 'expired'`` — but the snapshot is composed
    consistently for every trigger.
    """
    # Naive-UTC threshold to match the store's naive ``dismissed_at`` stamps
    # (alerts.store._now_iso); the SQL ``>=`` below is a lexicographic string
    # compare, so both sides must share the no-offset ISO format.
    naive_now = datetime.now(UTC).replace(tzinfo=None)
    threshold = (naive_now - timedelta(days=lookback_days)).isoformat()
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    try:
        try:
            rows = conn.execute(
                "SELECT signature_sha FROM alerts "
                "WHERE user_id = ? AND status = 'dismissed' "
                "AND dismissed_at >= ?",
                (user_id, threshold),
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {str(r[0]) for r in rows}
    finally:
        conn.close()


def _compose_user_state(db_path: Path, *, user_id: str, ticker: str) -> UserStateContext:
    """Snapshot the user-stated thesis state for ``ticker``.

    Best-effort: if the user_state tables are absent the snapshot
    degrades to empty lists / set rather than blocking the driver. The
    trigger's own ``should_fire`` decides how much of the context it
    cares about.
    """
    try:
        kpi_rows = list_kpis(user_id=user_id, ticker=ticker, db_path=db_path)
        kpis: list[dict[str, object]] = [_dc_to_dict(r) for r in kpi_rows]
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        kpis = []
    try:
        intent_rows = list_intents(user_id=user_id, ticker=ticker, db_path=db_path)
        intents: list[dict[str, object]] = [_dc_to_dict(r) for r in intent_rows]
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        intents = []
    dismissed = _load_dismissed_signatures(
        db_path, user_id=user_id, lookback_days=DISMISSED_LOOKBACK_DAYS
    )
    return UserStateContext(
        registered_kpis=kpis,
        sizing_intents=intents,
        recent_dismissed_signatures=dismissed,
    )


def _dc_to_dict(obj: object) -> dict[str, object]:
    """Render a (slotted) dataclass row as a plain dict for UserStateContext.

    The user_state CRUD modules return slotted dataclasses;
    UserStateContext takes ``list[dict[str, object]]`` so a future
    consumer can treat them uniformly without importing each row type.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return dict(obj.__dict__)


# --------------------------------------------------------------------------
# Cost tracking
# --------------------------------------------------------------------------


def query_run_cost_usd(db_path: Path, run_started_at: datetime) -> Decimal:
    """Sum ``cost_estimate_usd`` from ``llm_calls`` since the driver started.

    Returns ``Decimal('0')`` on any DB / table miss — the driver is allowed
    to proceed against a fresh repo. Defined at module scope so tests can
    monkeypatch it without touching the sqlite layer.
    """
    if not db_path.exists():
        return Decimal("0")
    threshold = run_started_at.isoformat()
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "llm_calls" not in tables:
                return Decimal("0")
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_estimate_usd), 0.0) FROM llm_calls WHERE called_at >= ?",
                (threshold,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning({"event": "run_cost_query_failed", "error": str(exc)})
        return Decimal("0")
    if row is None or row[0] is None:
        return Decimal("0")
    return Decimal(str(row[0]))


# --------------------------------------------------------------------------
# Trigger selection
# --------------------------------------------------------------------------


def _select_triggers(names: list[str] | None) -> list[RegisteredTrigger]:
    """Resolve the active trigger list from ``--triggers`` (or the registry default).

    When ``names`` is None (no CLI flag), returns ``ENABLED_TRIGGERS``.
    When ``names`` is supplied, looks up each by ``kind`` against
    ``ALL_TRIGGERS`` (so a caller can opt into a stub trigger for
    targeted runs); raises if any name is unknown so a typo halts the
    driver instead of silently dropping a trigger.
    """
    if names is None:
        return [item for item in ENABLED_TRIGGERS]
    by_kind = {cls.kind: cls for cls in ALL_TRIGGERS}
    out: list[RegisteredTrigger] = []
    for name in names:
        cls = by_kind.get(name)
        if cls is None:
            raise ValueError(f"unknown trigger kind: {name!r}. Available: {sorted(by_kind)}")
        out.append(cls)
    return out


# --------------------------------------------------------------------------
# Run summary
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _RunSummary:
    """End-of-run tally for the structured stdout report."""

    tickers_processed: int = 0
    alerts_fired: int = 0
    dedup_skips: int = 0
    stub_skips: int = 0
    no_candidate_skips: int = 0
    should_fire_skips: int = 0
    no_material_skips: int = 0
    dry_run_alerts: int = 0
    errors: int = 0
    cost_cap_reached: bool = False
    elapsed_seconds: float = 0.0
    started_at: str = ""
    ended_at: str = ""
    seen_tickers: set[str] = field(default_factory=set[str])


# --------------------------------------------------------------------------
# Per-candidate processing
# --------------------------------------------------------------------------


def _evidence_flags_no_material(evidence_json: str) -> bool:
    """True when the draft's own evidence says nothing material happened.

    The earnings_tone LLM diff sets ``no_material_shifts_detected: true`` when
    it found no shift worth flagging; ``draft_actions`` already respects the
    flag (queues nothing) but the ALERT itself used to persist anyway — a
    pending card whose memo says "nothing changed". Narrow evidence-flag
    check, not a trigger-protocol method: any trigger whose evidence carries
    the same flag gets the same skip.
    """
    try:
        payload = json.loads(evidence_json)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("no_material_shifts_detected") is True


def _alert_draft_to_json(draft: AlertDraft) -> dict[str, object]:
    """Render an AlertDraft as a stdout-friendly dict for ``--dry-run`` logging."""
    return {
        "trigger_kind": draft.trigger_kind,
        "ticker": draft.ticker,
        "fired_at": draft.fired_at.isoformat(),
        "evidence_json": draft.evidence_json,
        "signature_sha": draft.signature_sha,
        "memo_text": draft.memo_text,
    }


def _process_candidate(
    *,
    candidate: TriggerCandidate,
    trigger: Trigger,
    ticker: str,
    db_path: Path,
    user_id: str,
    user_state: UserStateContext,
    dry_run: bool,
    summary: _RunSummary,
) -> None:
    """Run the per-candidate pipeline. Mutates ``summary`` in place.

    Raises only on programming errors — every expected failure mode
    (LLM error, sqlite error, NotImplementedError from a stub) is
    caught and logged so the outer loop continues with the next
    candidate / ticker.
    """
    if not trigger.should_fire(candidate, user_state):
        _emit(
            {
                "event": "should_fire_false",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "candidate_key": candidate.key,
            }
        )
        summary.should_fire_skips += 1
        return

    key_evidence = trigger.signature_key_evidence(candidate)
    signature_sha = compute_signature_sha(trigger.kind, ticker, key_evidence)

    existing = find_by_signature(user_id=user_id, signature_sha=signature_sha, db_path=db_path)
    if existing is not None:
        _emit(
            {
                "event": "dedup_hit",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "signature_sha": signature_sha,
                "existing_alert_id": existing.id,
                "existing_status": existing.status,
            }
        )
        summary.dedup_skips += 1
        return

    # The ``anchor`` parameter on Trigger.build_alert is reserved for a
    # ThesisAnchor dataclass; today's sensors load their own markdown
    # anchor via load_thesis_anchor(repo_root, ticker) inside build_alert.
    # We pass None until a sensor adopts the structured-anchor consumption.
    try:
        alert_draft = trigger.build_alert(candidate, None)
    except NotImplementedError:
        _emit(
            {
                "event": "trigger_stub_skipping",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
            }
        )
        summary.stub_skips += 1
        return
    except Exception as exc:
        _emit(
            {
                "event": "build_alert_failed",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        summary.errors += 1
        return

    if _evidence_flags_no_material(alert_draft.evidence_json):
        _emit(
            {
                "event": "no_material_shifts_skip",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "signature_sha": alert_draft.signature_sha,
            }
        )
        summary.no_material_skips += 1
        return

    if dry_run:
        _emit(
            {
                "event": "dry_run_alert",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "draft": _alert_draft_to_json(alert_draft),
            }
        )
        summary.dry_run_alerts += 1
        return

    try:
        alert_row = fire_alert(
            user_id=user_id,
            ticker=alert_draft.ticker,
            trigger_kind=alert_draft.trigger_kind,
            fired_at=alert_draft.fired_at,
            evidence_json=alert_draft.evidence_json,
            signature_sha=alert_draft.signature_sha,
            db_path=db_path,
        )
    except Exception as exc:
        _emit(
            {
                "event": "fire_alert_failed",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        summary.errors += 1
        return

    action_drafts = trigger.draft_actions(alert_draft, candidate)
    persisted_actions = 0
    for action_draft in action_drafts:
        try:
            _ = queue_action(
                alert_id=alert_row.id,
                action_kind=action_draft.action_kind,
                payload=action_draft.payload,
                db_path=db_path,
            )
            persisted_actions += 1
        except Exception as exc:
            _emit(
                {
                    "event": "queue_action_failed",
                    "ticker": ticker,
                    "trigger_kind": trigger.kind,
                    "alert_id": alert_row.id,
                    "action_kind": action_draft.action_kind,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            summary.errors += 1

    _emit(
        {
            "event": "alert_fired",
            "ticker": ticker,
            "trigger_kind": trigger.kind,
            "alert_id": alert_row.id,
            "signature_sha": signature_sha,
            "n_actions": persisted_actions,
        }
    )
    summary.alerts_fired += 1


# --------------------------------------------------------------------------
# Per (ticker, trigger) wrapper
# --------------------------------------------------------------------------


def _process_ticker_trigger(
    *,
    trigger: Trigger | StatefulTrigger,
    ticker: str,
    db_path: Path,
    user_id: str,
    dry_run: bool,
    max_cost_usd: Decimal,
    run_started_at: datetime,
    summary: _RunSummary,
) -> bool:
    """Run one trigger against one ticker. Returns False iff the cost cap
    was reached mid-iteration and the driver should stop processing.

    Wraps scan + per-candidate dispatch in a single try/except so a
    sqlite blowup or unexpected exception in the trigger doesn't abort
    the whole driver — only this one (ticker, trigger) pair gets
    skipped. The summary still records the ticker as "seen" for the
    final processed-count.
    """
    summary.seen_tickers.add(ticker)
    if isinstance(trigger, StatefulTrigger):
        try:
            result = trigger.run(
                ticker=ticker,
                db_path=db_path,
                user_id=user_id,
                dry_run=dry_run,
            )
        except Exception as exc:
            _emit(
                {
                    "event": "stateful_trigger_failed",
                    "ticker": ticker,
                    "trigger_kind": trigger.kind,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            summary.errors += 1
            return True
        summary.alerts_fired += result.alerts_fired
        summary.dedup_skips += result.dedup_skips
        summary.dry_run_alerts += result.dry_run_alerts
        summary.no_candidate_skips += int(result.no_candidate)
        _emit(
            {
                "event": "stateful_trigger_complete",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "alerts_fired": result.alerts_fired,
                "no_candidate": result.no_candidate,
            }
        )
        return True
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            candidates = trigger.scan(ticker, conn)
        finally:
            conn.close()
    except Exception as exc:
        _emit(
            {
                "event": "scan_failed",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        summary.errors += 1
        return True

    if not candidates:
        _emit(
            {
                "event": "no_candidates",
                "ticker": ticker,
                "trigger_kind": trigger.kind,
            }
        )
        summary.no_candidate_skips += 1
        return True

    user_state = _compose_user_state(db_path, user_id=user_id, ticker=ticker)

    for candidate in candidates:
        current_cost = query_run_cost_usd(db_path, run_started_at)
        if current_cost >= max_cost_usd:
            _emit(
                {
                    "event": "cost_cap_reached",
                    "current_spend_usd": str(current_cost),
                    "cap_usd": str(max_cost_usd),
                    "ticker": ticker,
                    "trigger_kind": trigger.kind,
                }
            )
            summary.cost_cap_reached = True
            return False
        try:
            _process_candidate(
                candidate=candidate,
                trigger=trigger,
                ticker=ticker,
                db_path=db_path,
                user_id=user_id,
                user_state=user_state,
                dry_run=dry_run,
                summary=summary,
            )
        except Exception as exc:
            # Defense in depth: _process_candidate already catches its own
            # known failure modes. Anything else surfacing here is an
            # unexpected programming error — log, count, continue.
            _emit(
                {
                    "event": "candidate_unexpected_error",
                    "ticker": ticker,
                    "trigger_kind": trigger.kind,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
            summary.errors += 1
    return True


# --------------------------------------------------------------------------
# CLI plumbing
# --------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated ticker list. Default: portfolio + watchlist + evaluation "
        "from tracked_companies.",
    )
    parser.add_argument(
        "--triggers",
        default=None,
        help="Comma-separated trigger 'kind' strings to run. Default: ENABLED_TRIGGERS "
        "from triggers.registry (currently: earnings_tone). Stub kinds (kpi_inflection) "
        "are accepted via this flag and skipped on NotImplementedError.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute AlertDraft + log it as JSON but do NOT persist to alerts / "
        "queued_actions. Useful for verifying trigger output before letting it write.",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help=f"Cost cap for this run in USD (default {DEFAULT_MAX_COST_USD}). When "
        "accumulated llm_calls.cost_estimate_usd reaches the cap the driver halts "
        "cleanly with exit 0.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help=f"Owner of the alerts / user_state rows. Default: {DEFAULT_USER_ID!r}.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path. Defaults to db.DB_PATH "
        "(data/portfolio.db under the project root).",
    )
    return parser.parse_args(argv)


def _split_csv(raw: str | None) -> list[str] | None:
    """Split a comma-separated CLI value into a list of trimmed tokens.

    Empty string → empty list (lets a test exercise the empty-portfolio path
    by passing ``--tickers ''``). ``None`` → ``None`` (signals "use default").
    """
    if raw is None:
        return None
    return [tok.strip().upper() for tok in raw.split(",") if tok.strip()]


def _split_triggers(raw: str | None) -> list[str] | None:
    """Same shape as ``_split_csv`` but preserves trigger-name case."""
    if raw is None:
        return None
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)

    db_path = resolve_db_path(args.db_path)

    ticker_list = _split_csv(args.tickers)
    tickers = _load_portfolio_tickers(db_path) if ticker_list is None else ticker_list

    trigger_names = _split_triggers(args.triggers)
    trigger_classes = _select_triggers(trigger_names)
    triggers = [cls() for cls in trigger_classes]

    max_cost_usd = Decimal(str(args.max_cost_usd))
    run_started_at = datetime.now(UTC)
    t0 = time.monotonic()

    summary = _RunSummary(started_at=run_started_at.isoformat())

    _emit(
        {
            "event": "run_starting",
            "tickers": tickers,
            "triggers": [t.kind for t in triggers],
            "dry_run": args.dry_run,
            "max_cost_usd": str(max_cost_usd),
            "user_id": args.user_id,
            "db_path": str(db_path),
        }
    )

    halt = False
    for ticker in tickers:
        if halt:
            break
        for trigger in triggers:
            try:
                keep_going = _process_ticker_trigger(
                    trigger=trigger,
                    ticker=ticker,
                    db_path=db_path,
                    user_id=args.user_id,
                    dry_run=args.dry_run,
                    max_cost_usd=max_cost_usd,
                    run_started_at=run_started_at,
                    summary=summary,
                )
            except Exception as exc:
                # Outer defense-in-depth: only a true programming error
                # (e.g. a logic bug in _process_ticker_trigger itself)
                # reaches here. Log, count, continue.
                _emit(
                    {
                        "event": "ticker_trigger_unexpected_error",
                        "ticker": ticker,
                        "trigger_kind": trigger.kind,
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                    }
                )
                summary.errors += 1
                continue
            if not keep_going:
                halt = True
                break

    summary.tickers_processed = len(summary.seen_tickers)
    summary.elapsed_seconds = round(time.monotonic() - t0, 3)
    summary.ended_at = datetime.now(UTC).isoformat()

    sys.stdout.write(
        json.dumps(
            {
                "event": "run_summary",
                "tickers_processed": summary.tickers_processed,
                "alerts_fired": summary.alerts_fired,
                "dedup_skips": summary.dedup_skips,
                "stub_skips": summary.stub_skips,
                "no_candidate_skips": summary.no_candidate_skips,
                "should_fire_skips": summary.should_fire_skips,
                "no_material_skips": summary.no_material_skips,
                "dry_run_alerts": summary.dry_run_alerts,
                "errors": summary.errors,
                "cost_cap_reached": summary.cost_cap_reached,
                "elapsed_seconds": summary.elapsed_seconds,
                "started_at": summary.started_at,
                "ended_at": summary.ended_at,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

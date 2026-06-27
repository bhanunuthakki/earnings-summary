"""Standup watchers — the four deterministic trip detectors.

Each watcher is a pure read over the portfolio DB (the drift watcher also
consults the live tracker, falling back to the materialized-weights cache when
it is offline). None of them touch an LLM — the materiality judgment a standup
acts on must be reproducible from the audit trail, and the *quality* gate
(``standup.gate``) is where the LLM enters. A watcher that hits a missing table
/ column degrades to "no signals", never a crash: a half-migrated DB must not
take the morning pipeline down.

The four sources mirror the four open loops the standup closes:

  decision_condition  — a falsifiable "what would change my mind" condition the
                        analyst wrote has been satisfied (reuses the break-rule
                        evaluation of ``triggers.DecisionConditionTrigger`` so
                        the standup and the alert feed agree on what tripped).
  journal_open_item   — an open analyst note has gone unresolved past the stale
                        window (an open question that never got closed).
  dcf_staleness       — a held name's DCF was valued long ago AND the price has
                        since moved away from fair value (the model is old and
                        the gap it drives — trim ladder, next-dollar — is suspect).
  position_drift      — a held name's live weight has drifted off its stated
                        target weight (sizing no longer matches intent).

Each trip becomes a ``StandupSignal``: a stable ``signature`` (the dedup key
body), a deterministic one-line ``headline`` (the persisted user turn / feed
text), the structured ``evidence`` the framing + ledger snapshot off, and a
0..1 ``materiality`` used to rank which trips get composed first under the
per-run / per-day caps.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from standup.config import StandupConfig
from triggers.decision_condition import DecisionConditionTrigger

log = logging.getLogger(__name__)

# A book-level signal (a journal note with no ticker) keys its signature against
# this sentinel instead of a real symbol so the dedup hash stays stable.
BOOK_SCOPE = "__BOOK__"

# DCF upside past this is more likely a mis-modeled run (currency / unit
# artifacts on sweep-built watchlist DCFs) than a real gap — same ceiling and
# reasoning as ask.packs._IMPLAUSIBLE_UPSIDE_PCT / advisor.context.
_IMPLAUSIBLE_MISPRICING_PCT = 100.0

_NOTE_BODY_CHARS = 140

SIGNAL_KINDS: tuple[str, ...] = (
    "decision_condition",
    "decision_verdict_due",
    "journal_open_item",
    "dcf_staleness",
    "position_drift",
)


@dataclass(frozen=True, slots=True)
class StandupSignal:
    """One detected trip — see the module docstring for the field contract."""

    kind: str
    ticker: str | None
    signature: str  # stable within (kind, ticker); the dedup-key body
    headline: str
    materiality: float
    evidence: dict[str, object] = field(default_factory=dict[str, object])

    @property
    def scope_key(self) -> str:
        """The symbol used in the dedup signature — the ticker, or the
        book-level sentinel for a ticker-less (portfolio) signal."""
        return self.ticker or BOOK_SCOPE


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    """Best-effort rows; [] on a missing table / column (never raises)."""
    conn.row_factory = sqlite3.Row
    try:
        return cast("list[sqlite3.Row]", conn.execute(sql, params).fetchall())
    except sqlite3.Error:
        return []


def _age_days(iso: object, now: datetime) -> float | None:
    """Whole-and-fractional days between an ISO timestamp and ``now``.
    None when unparseable — the caller treats that as "age unknown" and skips."""
    if not isinstance(iso, str) or not iso.strip():
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "").strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    delta = now - parsed
    return max(0.0, delta.total_seconds() / 86400.0)


def _ramp(value: float, lo: float, hi: float) -> float:
    """Linear 0..1 ramp: 0 at/below ``lo``, 1 at/above ``hi``."""
    if hi <= lo:
        return 1.0 if value >= hi else 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _held_tickers(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """The portfolio (held) universe — what the DCF + drift watchers iterate."""
    rows = _rows(
        conn,
        "SELECT DISTINCT ticker FROM tracked_companies "
        "WHERE user_id = ? AND archived_at IS NULL AND list_type = 'portfolio' "
        "ORDER BY ticker",
        (user_id,),
    )
    return [str(r["ticker"]).upper() for r in rows if r["ticker"]]


# ---------------------------------------------------------------------------
# Watcher: decision_condition — the analyst's own falsifiability bar was hit
# ---------------------------------------------------------------------------


def _decision_condition_signals(
    conn: sqlite3.Connection, *, user_id: str, now: datetime, config: StandupConfig
) -> list[StandupSignal]:
    """Reuse the break-rule evaluation in ``DecisionConditionTrigger.scan`` so
    the standup and the alert feed never disagree on what tripped. A satisfied
    condition is the highest-materiality trip there is — the analyst pre-committed
    to it being decision-changing."""
    _ = (now, config)
    tickers = [
        str(r["ticker"]).upper()
        for r in _rows(
            conn,
            "SELECT DISTINCT ticker FROM decisions "
            "WHERE outcome_at IS NULL "
            "  AND decision_conditions IS NOT NULL AND decision_conditions != '[]'",
        )
        if r["ticker"]
    ]
    if not tickers:
        return []
    trigger = DecisionConditionTrigger()
    out: list[StandupSignal] = []
    for ticker in tickers:
        try:
            candidates = trigger.scan(ticker, conn)
        except sqlite3.Error:
            continue
        for cand in candidates:
            ev = dict(cand.evidence)
            label = str(ev.get("condition_label") or cand.key)
            summary = str(ev.get("decision_summary") or "an open decision")
            latest = ev.get("latest_value")
            latest_str = f"{latest:g}" if isinstance(latest, (int, float)) else "?"
            unit = str(ev.get("unit") or "")
            period = str(ev.get("period_end") or "?")
            headline = (
                f"{ticker}: a condition you set just tripped — {label} "
                f"(latest {latest_str} {unit} @ {period}). It was your bar for "
                f"{summary}."
            )
            out.append(
                StandupSignal(
                    kind="decision_condition",
                    ticker=ticker,
                    signature=cand.key,
                    headline=" ".join(headline.split()),
                    materiality=1.0,
                    evidence=ev,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Watcher: decision_verdict_due — an ungraded call whose horizon has elapsed
# ---------------------------------------------------------------------------


def _decision_verdict_due_signals(
    conn: sqlite3.Connection, *, user_id: str, now: datetime, config: StandupConfig
) -> list[StandupSignal]:
    """A recorded call whose review horizon has elapsed (made_at past the verdict
    window) but which was never graded (outcome_at NULL) — surfaced for an
    interactive verdict alongside the tripped-condition resurfacing. Mirrors
    ``decision_extractor.pending_for_grading`` (the grading cron's batch read) so
    the owner is nudged to close his OWN open calls, not just told what tripped.
    Lower materiality than a tripped condition — a nudge to grade, not an alert."""
    _ = user_id
    rows = _rows(
        conn,
        "SELECT id, ticker, recommendation_kind, recommendation_value, conviction, made_at "
        "FROM decisions WHERE outcome_at IS NULL ORDER BY made_at ASC",
    )
    out: list[StandupSignal] = []
    for r in rows:
        age = _age_days(r["made_at"], now)
        if age is None or age < config.decision_verdict_days:
            continue
        ticker = str(r["ticker"]).upper() if r["ticker"] else None
        kind = str(r["recommendation_kind"] or "").upper()
        val = r["recommendation_value"]
        size = f" {val:g}%" if isinstance(val, (int, float)) else ""
        conv = str(r["conviction"] or "").strip()
        conv_str = f", {conv} conviction" if conv else ""
        made_day = str(r["made_at"] or "")[:10]
        scope = ticker or "a call"
        headline = (
            f"{scope}: your {kind}{size} call from {made_day} ({round(age)}d ago{conv_str}) "
            "is due for a verdict — its horizon has elapsed and it's still ungraded. "
            "Did it play out?"
        )
        materiality = 0.3 + 0.5 * _ramp(
            age, config.decision_verdict_days, config.decision_verdict_days * 4
        )
        out.append(
            StandupSignal(
                kind="decision_verdict_due",
                ticker=ticker,
                signature=f"verdict_due:{int(r['id'])}",
                headline=" ".join(headline.split()),
                materiality=min(1.0, materiality),
                evidence={
                    "decision_id": int(r["id"]),
                    "recommendation_kind": kind,
                    "made_at": made_day,
                    "age_days": round(age, 1),
                    "conviction": conv or None,
                    "ticker": ticker,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Watcher: journal_open_item — an open note past the stale window
# ---------------------------------------------------------------------------


def _journal_signals(
    conn: sqlite3.Connection, *, user_id: str, now: datetime, config: StandupConfig
) -> list[StandupSignal]:
    rows = _rows(
        conn,
        "SELECT id, ticker, kind, body, created_at FROM analyst_notes "
        "WHERE user_id = ? AND status = 'open' ORDER BY created_at ASC",
        (user_id,),
    )
    out: list[StandupSignal] = []
    for r in rows:
        age = _age_days(r["created_at"], now)
        if age is None or age < config.journal_stale_days:
            continue
        ticker = str(r["ticker"]).upper() if r["ticker"] else None
        scope = ticker or "portfolio"
        body = " ".join(str(r["body"] or "").split())
        snippet = (
            body[: _NOTE_BODY_CHARS - 1].rstrip() + "…" if len(body) > _NOTE_BODY_CHARS else body
        )
        created_day = str(r["created_at"] or "")[:10]
        headline = (
            f"{scope}: an open journal {r['kind']} has gone unresolved since "
            f'{created_day} ({round(age)}d) — "{snippet}"'
        )
        materiality = 0.4 + 0.6 * _ramp(
            age, config.journal_stale_days, config.journal_stale_days * 3
        )
        out.append(
            StandupSignal(
                kind="journal_open_item",
                ticker=ticker,
                signature=f"note:{int(r['id'])}",
                headline=" ".join(headline.split()),
                materiality=materiality,
                evidence={
                    "note_id": int(r["id"]),
                    "note_kind": str(r["kind"]),
                    "body": body,
                    "created_at": str(r["created_at"]),
                    "age_days": round(age, 1),
                    "ticker": ticker,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Watcher: dcf_staleness — old assumptions AND the price has run away
# ---------------------------------------------------------------------------


def _latest_dcf(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    rows = _rows(
        conn,
        "SELECT ticker, valuation_date, npv_per_share, live_price "
        "FROM dcf_runs WHERE UPPER(ticker) = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (ticker,),
    )
    return rows[0] if rows else None


def _dcf_staleness_signals(
    conn: sqlite3.Connection, *, user_id: str, now: datetime, config: StandupConfig
) -> list[StandupSignal]:
    out: list[StandupSignal] = []
    for ticker in _held_tickers(conn, user_id):
        row = _latest_dcf(conn, ticker)
        if row is None:
            continue
        age = _age_days(row["valuation_date"], now)
        try:
            fv = float(row["npv_per_share"]) if row["npv_per_share"] is not None else None
            px = float(row["live_price"]) if row["live_price"] is not None else None
        except (TypeError, ValueError):
            fv = px = None
        if age is None or age < config.dcf_stale_days:
            continue
        if fv is None or px is None or fv <= 0 or px <= 0:
            continue  # can't assess the gap → don't trip on staleness alone
        mispricing = (px / fv - 1.0) * 100.0  # convention-proof; +ve = over fair
        if abs(mispricing) < config.dcf_mispricing_pct:
            continue
        if abs(mispricing) > _IMPLAUSIBLE_MISPRICING_PCT:
            continue  # a mis-modeled run, not a real gap
        valuation_day = str(row["valuation_date"] or "")[:10]
        headline = (
            f"{ticker}: DCF was valued {valuation_day} ({round(age)}d ago) and price "
            f"is now {mispricing:+.0f}% vs fair ${fv:,.2f} — the assumptions may be stale."
        )
        materiality = (
            0.3
            + 0.4 * _ramp(age, config.dcf_stale_days, config.dcf_stale_days * 3)
            + 0.3 * _ramp(abs(mispricing), config.dcf_mispricing_pct, config.dcf_mispricing_pct * 3)
        )
        out.append(
            StandupSignal(
                kind="dcf_staleness",
                ticker=ticker,
                signature=f"dcf:{ticker}:{valuation_day}",
                headline=" ".join(headline.split()),
                materiality=min(1.0, materiality),
                evidence={
                    "valuation_date": valuation_day,
                    "age_days": round(age, 1),
                    "npv_per_share": round(fv, 4),
                    "live_price": round(px, 4),
                    "mispricing_pct": round(mispricing, 1),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Watcher: position_drift — live weight off the stated target
# ---------------------------------------------------------------------------

_TARGET_WEIGHT_KIND = "target_weight_pct"  # matches pipeline.allocation_decisions_panel


def _current_weights_pct(conn: sqlite3.Connection, repo_root: Path | None) -> dict[str, float]:
    """ticker → live weight in PERCENT. Prefers the tracker; falls back to the
    materialized-weights disk cache (fractions → percent) when it is offline, so
    the watcher degrades to last-known weights instead of going dark."""
    _ = conn
    try:
        from integrations.portfolio_tracker_client import fetch_live_portfolio

        live = fetch_live_portfolio()
        if live.available:
            return {
                (p.ticker or "").upper(): float(p.percent_of_portfolio)
                for p in live.positions
                if p.ticker and p.percent_of_portfolio is not None
            }
    except Exception:  # tracker import / call is best-effort
        log.debug({"event": "standup_drift_tracker_unavailable"}, exc_info=True)
    if repo_root is None:
        return {}
    try:
        from portfolio_weights import read_materialized_weights

        return {t: v * 100.0 for t, v in read_materialized_weights(repo_root).items()}
    except Exception:
        log.debug({"event": "standup_drift_cache_unavailable"}, exc_info=True)
        return {}


def _position_drift_signals(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    now: datetime,
    config: StandupConfig,
    repo_root: Path | None,
) -> list[StandupSignal]:
    _ = now
    targets = _rows(
        conn,
        "SELECT ticker, intent_value, created_at FROM position_sizing_intent "
        "WHERE user_id = ? AND intent_kind = ? AND intent_value IS NOT NULL "
        "ORDER BY created_at DESC, id DESC",
        (user_id, _TARGET_WEIGHT_KIND),
    )
    latest_target: dict[str, float] = {}
    for r in targets:  # newest-first → first per ticker wins
        t = str(r["ticker"]).upper()
        if t not in latest_target:
            try:
                latest_target[t] = float(r["intent_value"])
            except (TypeError, ValueError):
                continue
    if not latest_target:
        return []
    weights = _current_weights_pct(conn, repo_root)
    source = "tracker_or_cache"
    out: list[StandupSignal] = []
    for ticker, target_pct in latest_target.items():
        current = weights.get(ticker)
        if current is None:
            continue  # no live or cached weight → can't assess drift, stay quiet
        drift = current - target_pct
        if abs(drift) < config.drift_min_pp:
            continue
        direction = "over" if drift > 0 else "under"
        headline = (
            f"{ticker}: now {current:.1f}% of the book vs your {target_pct:g}% target "
            f"({drift:+.1f}pp, {direction}weight)."
        )
        materiality = 0.3 + 0.7 * _ramp(abs(drift), config.drift_min_pp, config.drift_min_pp * 3)
        out.append(
            StandupSignal(
                kind="position_drift",
                ticker=ticker,
                signature=f"drift:{ticker}:{target_pct:g}:{direction}",
                headline=" ".join(headline.split()),
                materiality=min(1.0, materiality),
                evidence={
                    "current_pct": round(current, 2),
                    "target_pct": target_pct,
                    "drift_pp": round(drift, 2),
                    "weight_source": source,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def collect_signals(
    conn: sqlite3.Connection,
    *,
    user_id: str = DEFAULT_USER_ID,
    repo_root: Path | None = None,
    now: datetime,
    config: StandupConfig | None = None,
) -> list[StandupSignal]:
    """Run every watcher (each independently best-effort) and return the trips
    ranked by materiality, highest first — the order they are considered for
    composition under the per-run / per-day caps. One watcher raising never
    sinks the others."""
    cfg = config or StandupConfig()
    signals: list[StandupSignal] = []

    def _safe(label: str, fn: Callable[[], list[StandupSignal]]) -> None:
        try:
            signals.extend(fn())
        except Exception:
            log.warning({"event": "standup_watcher_failed", "watcher": label}, exc_info=True)

    _safe(
        "decision_condition",
        lambda: _decision_condition_signals(conn, user_id=user_id, now=now, config=cfg),
    )
    _safe(
        "decision_verdict_due",
        lambda: _decision_verdict_due_signals(conn, user_id=user_id, now=now, config=cfg),
    )
    _safe("journal_open_item", lambda: _journal_signals(conn, user_id=user_id, now=now, config=cfg))
    _safe(
        "dcf_staleness",
        lambda: _dcf_staleness_signals(conn, user_id=user_id, now=now, config=cfg),
    )
    _safe(
        "position_drift",
        lambda: _position_drift_signals(
            conn, user_id=user_id, now=now, config=cfg, repo_root=repo_root
        ),
    )

    filtered = [s for s in signals if s.materiality >= cfg.min_materiality]
    filtered.sort(key=lambda s: (-s.materiality, s.kind, s.ticker or ""))
    return filtered[: cfg.max_signals_considered]


__all__ = ["BOOK_SCOPE", "SIGNAL_KINDS", "StandupSignal", "collect_signals"]

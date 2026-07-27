"""Senior Partner Brief (P2.2, ``docs/design/personal_investment_partner_prd.md``
§9.1, §3.3 ownership rule). The one primary proactive advisory experience:
five ordered sections composed weekly over the whole advisory surface.

**Ownership rule (owner-ratified, PRD §9.1/§3.3): the brief owns DELIVERY;
the tenet-2/governor machinery owns DETECTION.** The four PRD moment classes
(``calibration_finding``, ``capacity_breach``, ``profile_drift``,
``life_event_checkpoint`` — ``research.governor.BRIEF_ROUTED_CLASSES``) stop
delivering as standalone Telegram pings; the governor routes them to
``coach_pings.status='routed_to_brief'`` instead of sending, and
:func:`compose_brief` drains every undrained routed row
(``research.governor.pending_routed_to_brief``) into section 4/5 candidates,
then marks them briefed (``research.governor.mark_pings_briefed``). Only
tier-1 ``decisive_alert_reason`` events still deliver immediately outside the
brief — that path is untouched by this module. Governor caps, cooldowns, and
dismiss/mute learning are RETAINED and continue to gate the four classes
BEFORE they ever reach ``routed_to_brief`` (freshness + mute checks still
run in ``run_governor``); what changed is only the delivery leg.

Flow (see :func:`compose_brief`):

1. Deterministic inputs FIRST — the latest ``incremental_dollar_recommendation``
   artifact, the latest VALID ``RiskSnapshot`` (``portfolio_risk_snapshot_store.
   read_latest_snapshot``, "valid" = a core metric is populated — PRD §4.1 found
   the latest row can carry null core metrics), the latest
   ``wealth_context_snapshot_history`` row, current Investment Decision Cards
   (fresh vs. stale by a 14-day cutoff), this ISO week's open weekly-packet
   items, undrained governor-routed moments (the four classes above), the one
   ``v_decision_journal`` row best fitting "a prior Owner Decision worth
   revisiting" (``advice_preceded=1``, not yet process-graded), Worldview +
   owner-profile anchors (spotlight-wrapped — untrusted-shaped owner text),
   and open research proposals. A deterministic active-week detector (earnings
   cluster / fresh diligence on multiple evaluation names / a real position
   change this week) decides whether the brief may exceed 3 action_requested
   items.
2. ONE governed structured call (``call_llm_structured``,
   purpose=``senior_partner_brief``) composes the five sections' JUDGMENT text
   from those grounded facts.
3. ``model_validate`` + :meth:`SeniorPartnerBrief.validate_notification_policy`
   (<=3 ``action_requested`` items unless ``is_active_week`` AND
   ``active_week_explanation`` is populated) + :meth:`SeniorPartnerBrief.
   validate_grounding` (every ``source_refs`` entry must resolve against the
   gathered inputs). Failures feed back into ONE corrective retry; still
   failing routes to the deterministic fallback.
4. Persist via ``llm_artifact_store.upsert`` — scope='portfolio',
   purpose='senior_partner_brief', cached on (iso_week, every gathered
   artifact/snapshot/ping id) so a same-week re-run with unchanged inputs is a
   cache hit (no re-spend) — the CLI's idempotency-per-ISO-week contract.
5. On successful persistence, drain the routed governor pings that fed this
   brief (``mark_pings_briefed``) — they are now "seen," never re-drained.

Error handling (mirrors ``allocation.recommendation_artifact`` /
``research.investment_decision_card``): ``LLMBudgetExceeded`` and transient
LLM failures degrade to a LABELED deterministic fallback — a mechanical
digest of artifact pointers, explicitly marked, with every item
``disposition='context_only'`` (never ``action_requested`` — no LLM judgment
ran to justify prioritizing one action over another, so the fallback claims
none). Any ``is_hard_stop`` exception (auth/setup) propagates loud.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from pydantic import BaseModel, Field

import llm_artifact_store
from llm.cli import LLMBudgetExceeded, is_hard_stop
from llm.prompt_versions import prompt_version_for
from llm.structured import StructuredParseError, call_llm_structured
from llm.untrusted import spotlight
from runtime.secrets import secret_file

PURPOSE = "senior_partner_brief"
ENGINE_VERSION = "v1"

# Investment Decision Card freshness cutoff (days) — a card older than this
# renders as "stale" in the composed inputs rather than silently looking
# current. Deliberately simple (age-based, not an input_sha re-derivation)
# given this module's scope is COMPOSITION, not card freshness itself.
_CARD_FRESH_DAYS = 14

_PRIVATE_BASE_URL_PATH = secret_file(
    "private_mobile_base_url",
    repo_root=Path(__file__).resolve().parents[2],
)

# "Active week" thresholds (PRD §9.1: "an active week increases visible
# context, not ping frequency") — all deterministic, all over a rolling
# 7-day window ending at compose time.
_ACTIVE_WEEK_WINDOW_DAYS = 7
_EARNINGS_CLUSTER_MIN = 2
_FRESH_CARD_ACTIVE_MIN = 2

_DISPOSITIONS: tuple[str, ...] = (
    "action_requested",
    "context_only",
    "blocked_missing_evidence",
    "no_action_warranted",
)
_MAX_NORMAL_WEEK_ACTIONS = 3

__all__ = [
    "ACTIVE_WEEK_WINDOW_DAYS",
    "MAX_NORMAL_WEEK_ACTIONS",
    "PURPOSE",
    "BriefItem",
    "BriefResult",
    "SeniorPartnerBrief",
    "build_telegram_keyboard",
    "build_telegram_text",
    "compose_brief",
    "dismiss_routed_moment",
    "render_markdown",
]

# Public re-exports of the module constants above (PRD-facing names).
ACTIVE_WEEK_WINDOW_DAYS = _ACTIVE_WEEK_WINDOW_DAYS
MAX_NORMAL_WEEK_ACTIONS = _MAX_NORMAL_WEEK_ACTIONS


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


class BriefItem(BaseModel):
    """One item in any of the five sections. ``effort_estimate`` is populated
    only for ``action_requested`` items (PRD §9.1 "effort estimate per
    action") — left ``None`` for context/blocked/no-action items, which have
    no action to estimate."""

    title: str = Field(min_length=1)
    body: str = ""
    disposition: Literal[
        "action_requested", "context_only", "blocked_missing_evidence", "no_action_warranted"
    ]
    effort_estimate: Literal["quick", "moderate", "substantial"] | None = None
    ticker: str | None = None
    source_refs: list[str] = Field(default_factory=list[str])


class SeniorPartnerBrief(BaseModel):
    """The full §9.1 structured output — five ordered sections. Sections 2-5
    are single items (or ``None`` when nothing qualifies that week); section 1
    is a list ("what changed that matters" is inherently plural)."""

    as_of: str = Field(min_length=1)
    iso_year: int
    iso_week: int
    input_sha: str = Field(min_length=1)
    engine_version: str = ENGINE_VERSION
    prompt_version: str = "v1"
    selection_mode: Literal["llm", "deterministic_fallback"] = "llm"

    what_changed: list[BriefItem] = Field(default_factory=list[BriefItem])
    highest_priority_decision: BriefItem | None = None
    capital_use: BriefItem | None = None
    assumption_challenge: BriefItem | None = None
    decision_revisit: BriefItem | None = None

    is_active_week: bool = False
    active_week_reasons: list[str] = Field(default_factory=list[str])
    active_week_explanation: str = ""

    source_refs: list[str] = Field(default_factory=list[str])
    # coach_pings ids this brief drained (P2.2 governor adapter) — audit trail
    # + the exact set compose_brief marks briefed after a successful persist.
    routed_ping_ids: list[int] = Field(default_factory=list[int])

    def all_items(self) -> list[BriefItem]:
        items = list(self.what_changed)
        for single in (
            self.highest_priority_decision,
            self.capital_use,
            self.assumption_challenge,
            self.decision_revisit,
        ):
            if single is not None:
                items.append(single)
        return items

    def action_requested_items(self) -> list[BriefItem]:
        return [i for i in self.all_items() if i.disposition == "action_requested"]

    def validate_notification_policy(self) -> list[str]:
        """PRD §9.1 deterministic admission rule: normal weeks contain at most
        ``MAX_NORMAL_WEEK_ACTIONS`` action_requested items. An active week MAY
        exceed it only with ``active_week_explanation`` populated — the
        notification-frequency invariant lives outside this model (the CLI
        still sends exactly one weekly message regardless of item count;
        "active" widens visible CONTEXT, never ping frequency)."""
        reasons: list[str] = []
        n = len(self.action_requested_items())
        if n > MAX_NORMAL_WEEK_ACTIONS:
            if not self.is_active_week:
                reasons.append(
                    f"{n} action_requested items but is_active_week=False "
                    f"(max {MAX_NORMAL_WEEK_ACTIONS} in a normal week)"
                )
            elif not self.active_week_explanation.strip():
                reasons.append(
                    f"{n} action_requested items in an active week requires a "
                    "populated active_week_explanation"
                )
        return reasons

    def validate_grounding(self, *, allowed_refs: set[str]) -> list[str]:
        """Every cited ``source_refs`` entry must trace to a gathered input —
        never an invented citation (mirrors InvestmentDecisionCard /
        IncrementalDollarRecommendation's grounding checks)."""
        reasons: list[str] = []
        cited: set[str] = set(self.source_refs)
        for item in self.all_items():
            cited.update(item.source_refs)
        for ref in cited:
            if ref in allowed_refs:
                continue
            if any(ref in allowed for allowed in allowed_refs):
                continue
            reasons.append(f"source_refs cites {ref!r}, not present in the gathered inputs")
        return reasons


@dataclass(frozen=True, slots=True)
class BriefResult:
    """The generator's output — always populated except a genuine hard stop."""

    artifact_id: int | None
    brief: SeniorPartnerBrief
    cache_hit: bool
    selection_mode: Literal["llm", "deterministic_fallback"] = "llm"
    degraded_reasons: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Deterministic input gathering
# --------------------------------------------------------------------------- #


def _sha(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ro_conn(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True, slots=True)
class _RecommendationInput:
    line: str | None
    ref: str | None


def _gather_recommendation(db_path: Path) -> _RecommendationInput:
    try:
        artifact = llm_artifact_store.read_current(
            ticker=None,
            purpose="incremental_dollar_recommendation",
            scope="portfolio",
            db_path=db_path,
        )
    except Exception:
        return _RecommendationInput(None, None)
    if artifact is None or not isinstance(artifact.content_json, dict):
        return _RecommendationInput(None, None)
    content = cast("dict[str, object]", artifact.content_json)
    status = str(content.get("status") or "")
    hypothesis = str(content.get("central_hypothesis") or "")[:400]
    ref = f"incremental_dollar_recommendation:artifact:{artifact.id}"
    line = f"[{ref}] status={status}: {hypothesis}"
    return _RecommendationInput(line, ref)


@dataclass(frozen=True, slots=True)
class _RiskInput:
    line: str | None
    ref: str | None


def _valid_risk_snapshot(snap: object) -> bool:
    """PRD §4.1 finding: the latest risk snapshot row can carry NULL core
    metrics — "valid" requires at least one core metric populated, not merely
    a row existing."""
    beta = getattr(snap, "beta", None)
    sharpe = getattr(snap, "sharpe", None)
    vol = getattr(snap, "portfolio_volatility_annualized", None)
    return any(v is not None for v in (beta, sharpe, vol))


def _gather_risk(db_path: Path) -> _RiskInput:
    try:
        from portfolio_risk_snapshot_store import read_latest_snapshot

        snap = read_latest_snapshot(db_path=db_path)
    except Exception:
        return _RiskInput(None, None)
    if snap is None or not _valid_risk_snapshot(snap):
        return _RiskInput(None, None)
    ref = f"risk_snapshot:{snap.captured_at}"
    parts = [
        f"beta={snap.beta:.2f}" if snap.beta is not None else "beta=?",
        f"sharpe={snap.sharpe:.2f}" if snap.sharpe is not None else "sharpe=?",
        f"top1={snap.top1_weight_pct:.1f}%" if snap.top1_weight_pct is not None else "top1=?",
        f"hhi={snap.hhi:.3f}" if snap.hhi is not None else "hhi=?",
        (
            f"drawdown={snap.current_drawdown_pct:.1f}%"
            if snap.current_drawdown_pct is not None
            else "drawdown=?"
        ),
    ]
    line = f"[{ref}] captured {snap.captured_at}: " + ", ".join(parts)
    return _RiskInput(line, ref)


@dataclass(frozen=True, slots=True)
class _WealthInput:
    line: str | None
    ref: str | None


def _gather_wealth(db_path: Path) -> _WealthInput:
    try:
        import wealth_context_store

        row = wealth_context_store.read_latest(db_path=db_path)
    except Exception:
        return _WealthInput(None, None)
    if row is None:
        return _WealthInput(None, None)
    ref = f"wealth_context:{row.as_of}"
    # PRD §11.4 privacy: dollar TOTALS never leave the Tailscale-protected web
    # surface via Telegram — but this line only feeds the LLM prompt (a
    # trusted, private-surface composition step); build_telegram_text below
    # is the actual privacy boundary and strips totals independently.
    line = (
        f"[{ref}] as of {row.as_of} (tracker as-of {row.tracker_as_of or 'unknown'}): "
        f"net_worth_total={row.net_worth_total} {row.currency}, "
        f"liquid_total={row.liquid_total} {row.currency}"
    )
    return _WealthInput(line, ref)


@dataclass(frozen=True, slots=True)
class _CardsInput:
    lines: list[str]
    refs: list[str]


def _gather_cards(db_path: Path, *, now: datetime) -> _CardsInput:
    conn = _ro_conn(db_path)
    if conn is None:
        return _CardsInput([], [])
    cutoff = (now - timedelta(days=_CARD_FRESH_DAYS)).isoformat()
    try:
        rows = conn.execute(
            """
            SELECT la.id AS artifact_id, la.ticker AS ticker, la.generated_at AS generated_at,
                   la.content_json AS content_json
            FROM llm_artifacts la
            JOIN tracked_companies tc ON UPPER(tc.ticker) = UPPER(la.ticker)
            WHERE la.purpose = 'investment_decision_card'
              AND la.scope = 'ticker'
              AND la.superseded_by_id IS NULL
              AND tc.list_type = 'evaluation'
            ORDER BY la.generated_at DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return _CardsInput([], [])
    finally:
        conn.close()
    lines: list[str] = []
    refs: list[str] = []
    for r in rows:
        ref = f"investment_decision_card:artifact:{int(r['artifact_id'])}"
        freshness = "fresh" if str(r["generated_at"] or "") >= cutoff else "stale"
        disposition = ""
        try:
            content_raw: object = json.loads(r["content_json"] or "{}")
            if isinstance(content_raw, dict):
                disposition = str(
                    cast("dict[str, object]", content_raw).get("suggested_disposition") or ""
                )
        except (TypeError, ValueError):
            disposition = ""
        lines.append(
            f"[{ref}] {r['ticker']} ({freshness}, generated {r['generated_at']}): "
            f"suggested_disposition={disposition or 'unknown'}"
        )
        refs.append(ref)
    return _CardsInput(lines, refs)


@dataclass(frozen=True, slots=True)
class _PacketInput:
    lines: list[str]
    refs: list[str]


def _gather_packet_items(db_path: Path, *, now: datetime) -> _PacketInput:
    conn = _ro_conn(db_path)
    if conn is None:
        return _PacketInput([], [])
    iso_year, iso_week, _ = now.isocalendar()
    try:
        rows = conn.execute(
            """
            SELECT wi.id AS item_id, wi.item_kind AS item_kind, wi.ticker AS ticker,
                   wi.title AS title
            FROM weekly_packet_items wi
            JOIN weekly_packet_runs wr ON wr.id = wi.run_id
            WHERE wr.iso_year = ? AND wr.iso_week = ? AND wi.verdict IS NULL
            ORDER BY wi.order_index
            """,
            (iso_year, iso_week),
        ).fetchall()
    except sqlite3.Error:
        return _PacketInput([], [])
    finally:
        conn.close()
    lines: list[str] = []
    refs: list[str] = []
    for r in rows:
        ref = f"weekly_packet_item:{int(r['item_id'])}"
        ticker_note = f" ({r['ticker']})" if r["ticker"] else ""
        lines.append(f"[{ref}] {r['item_kind']}{ticker_note}: {r['title']}")
        refs.append(ref)
    return _PacketInput(lines, refs)


@dataclass(frozen=True, slots=True)
class _RoutedMomentLine:
    ping_id: int
    class_: str
    ticker: str | None
    ref: str
    line: str


def _gather_routed_moments(db_path: Path) -> list[_RoutedMomentLine]:
    try:
        from research.governor import pending_routed_to_brief

        pending = pending_routed_to_brief(db_path)
    except Exception:
        return []
    out: list[_RoutedMomentLine] = []
    for p in pending:
        ref = f"coach_ping:{p.id}"
        ticker_note = f" ({p.ticker})" if p.ticker else ""
        out.append(
            _RoutedMomentLine(
                ping_id=p.id,
                class_=p.class_,
                ticker=p.ticker,
                ref=ref,
                line=f"[{ref}] {p.class_}{ticker_note}",
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class _PriorDecisionInput:
    line: str | None
    ref: str | None


def _gather_prior_decision(db_path: Path) -> _PriorDecisionInput:
    conn = _ro_conn(db_path)
    if conn is None:
        return _PriorDecisionInput(None, None)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name='v_decision_journal'"
        ).fetchone()
        if present is None:
            return _PriorDecisionInput(None, None)
        row = conn.execute(
            """
            SELECT decision_id, ticker, recommendation_kind, made_at, falsifier
            FROM v_decision_journal
            WHERE decided_by = 'owner' AND advice_preceded = 1 AND process_quality IS NULL
            ORDER BY made_at DESC LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return _PriorDecisionInput(None, None)
    finally:
        conn.close()
    if row is None:
        return _PriorDecisionInput(None, None)
    ref = f"decision:{int(row['decision_id'])}"
    falsifier = f" falsifier: {row['falsifier']}" if row["falsifier"] else ""
    line = (
        f"[{ref}] {row['recommendation_kind']} {row['ticker']} on {row['made_at']} — "
        f"advice preceded this decision, no process reflection recorded yet.{falsifier}"
    )
    return _PriorDecisionInput(line, ref)


def _gather_proposals(db_path: Path) -> list[str]:
    try:
        from research.proposals import list_proposals

        proposals = list_proposals(status="pending", db_path=db_path)
    except Exception:
        return []
    return [
        f"[research_proposal:{p.id}] {p.title}" + (f" ({p.ticker})" if p.ticker else "")
        for p in proposals[:10]
    ]


def _gather_anchors(repo_root: Path) -> str:
    try:
        from llm.anchors import load_owner_profile_anchor, load_worldview_anchor

        worldview = load_worldview_anchor(repo_root)
        owner_profile = load_owner_profile_anchor(repo_root)
    except Exception:
        return ""
    block = "\n\n".join(a for a in (worldview, owner_profile) if a)
    if not block:
        return ""
    return spotlight(block, source="worldview_owner_profile_anchors")


def _earnings_cluster_count(db_path: Path, *, now: datetime) -> int:
    conn = _ro_conn(db_path)
    if conn is None:
        return 0
    window_end = (now + timedelta(days=_ACTIVE_WEEK_WINDOW_DAYS)).date().isoformat()
    window_start = now.date().isoformat()
    try:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT e.ticker) FROM expected_earnings e
            JOIN tracked_companies tc ON UPPER(tc.ticker) = UPPER(e.ticker)
            WHERE tc.list_type = 'portfolio'
              AND e.expected_date BETWEEN ? AND ?
            """,
            (window_start, window_end),
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _portfolio_shift_count(db_path: Path, *, now: datetime) -> int:
    conn = _ro_conn(db_path)
    if conn is None:
        return 0
    cutoff = (now - timedelta(days=_ACTIVE_WEEK_WINDOW_DAYS)).isoformat()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM decisions
            WHERE decided_by = 'owner' AND created_at >= ?
              AND recommendation_kind NOT IN ('pass', 'watch', 'promote')
            """,
            (cutoff,),
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _detect_active_week(
    db_path: Path, *, now: datetime, fresh_card_count: int
) -> tuple[bool, list[str]]:
    """PRD §9.1: "an active week increases visible context, not ping
    frequency" — earnings cluster / active new-position evaluation / a
    portfolio shift, all deterministic over a rolling 7-day window."""
    reasons: list[str] = []
    earnings_n = _earnings_cluster_count(db_path, now=now)
    if earnings_n >= _EARNINGS_CLUSTER_MIN:
        reasons.append(f"earnings cluster: {earnings_n} portfolio names report within 7 days")
    if fresh_card_count >= _FRESH_CARD_ACTIVE_MIN:
        reasons.append(
            f"active new-position evaluation: {fresh_card_count} fresh Investment Decision "
            "Cards in the last 14 days"
        )
    shift_n = _portfolio_shift_count(db_path, now=now)
    if shift_n >= 1:
        reasons.append(f"portfolio shift: {shift_n} owner-executed decision(s) in the last 7 days")
    return bool(reasons), reasons


@dataclass(frozen=True, slots=True)
class _Inputs:
    as_of: str
    iso_year: int
    iso_week: int
    recommendation: _RecommendationInput
    risk: _RiskInput
    wealth: _WealthInput
    cards: _CardsInput
    packet: _PacketInput
    routed: list[_RoutedMomentLine]
    prior_decision: _PriorDecisionInput
    proposals: list[str]
    anchors_block: str
    is_active_week: bool
    active_week_reasons: list[str]
    allowed_refs: set[str]
    input_sha: str
    cache_inputs: list[str]


def _gather_inputs(db_path: Path, repo_root: Path, *, now: datetime) -> _Inputs:
    iso_year, iso_week, _ = now.isocalendar()
    recommendation = _gather_recommendation(db_path)
    risk = _gather_risk(db_path)
    wealth = _gather_wealth(db_path)
    cards = _gather_cards(db_path, now=now)
    packet = _gather_packet_items(db_path, now=now)
    routed = _gather_routed_moments(db_path)
    prior_decision = _gather_prior_decision(db_path)
    proposals = _gather_proposals(db_path)
    anchors_block = _gather_anchors(repo_root)

    fresh_card_count = sum(1 for line in cards.lines if "(fresh," in line)
    is_active_week, active_week_reasons = _detect_active_week(
        db_path, now=now, fresh_card_count=fresh_card_count
    )

    allowed_refs: set[str] = set()
    for candidate in (recommendation.ref, risk.ref, wealth.ref, prior_decision.ref):
        if candidate:
            allowed_refs.add(candidate)
    allowed_refs.update(cards.refs)
    allowed_refs.update(packet.refs)
    allowed_refs.update(m.ref for m in routed)
    allowed_refs.update(p.split("]")[0][1:] for p in proposals if p.startswith("["))

    cache_inputs = [
        f"{iso_year}-W{iso_week:02d}",
        recommendation.ref or "",
        risk.ref or "",
        wealth.ref or "",
        prior_decision.ref or "",
        *sorted(cards.refs),
        *sorted(packet.refs),
        *sorted(m.ref for m in routed),
    ]
    payload = {"cache_inputs": cache_inputs}
    return _Inputs(
        as_of=now.isoformat(),
        iso_year=int(iso_year),
        iso_week=int(iso_week),
        recommendation=recommendation,
        risk=risk,
        wealth=wealth,
        cards=cards,
        packet=packet,
        routed=routed,
        prior_decision=prior_decision,
        proposals=proposals,
        anchors_block=anchors_block,
        is_active_week=is_active_week,
        active_week_reasons=active_week_reasons,
        allowed_refs=allowed_refs,
        input_sha=_sha(payload),
        cache_inputs=cache_inputs,
    )


# --------------------------------------------------------------------------- #
# Prompt composition
# --------------------------------------------------------------------------- #


def _build_prompt(inputs: _Inputs, *, corrective_reasons: list[str] | None = None) -> str:
    rec_block = inputs.recommendation.line or "(no Incremental Dollar Recommendation on file)"
    risk_block = inputs.risk.line or "(no valid Risk Budget snapshot on file)"
    wealth_block = inputs.wealth.line or "(no wealth context snapshot on file)"
    cards_block = (
        "\n".join(f"- {ln}" for ln in inputs.cards.lines) or "(no evaluation cards on file)"
    )
    packet_block = (
        "\n".join(f"- {ln}" for ln in inputs.packet.lines) or "(no open weekly-packet items)"
    )
    routed_block = (
        "\n".join(f"- {m.line}" for m in inputs.routed) or "(no governor-routed moments waiting)"
    )
    prior_block = inputs.prior_decision.line or "(no advice-preceded decision awaiting reflection)"
    proposals_block = (
        "\n".join(f"- {p}" for p in inputs.proposals) or "(no open research proposals)"
    )
    active_block = (
        f"ACTIVE WEEK — reasons: {'; '.join(inputs.active_week_reasons)}"
        if inputs.is_active_week
        else "NOT an active week — the <=3 action_requested cap is HARD."
    )
    corrective_block = ""
    if corrective_reasons:
        corrective_block = (
            "\n\nYOUR PRIOR ATTEMPT FAILED THESE CHECKS — fix every one:\n"
            + "\n".join(f"- {r}" for r in corrective_reasons)
        )

    return f"""You are composing this week's Senior Partner Brief — the ONE weekly
proactive advisory synthesis for a solo owner running a personal equity
portfolio on roughly 5 hours/week of attention.

INCREMENTAL DOLLAR RECOMMENDATION:
{rec_block}

RISK BUDGET (latest valid snapshot):
{risk_block}

WEALTH CONTEXT (aggregates only):
{wealth_block}

INVESTMENT DECISION CARDS (evaluation-list names):
{cards_block}

OPEN WEEKLY-PACKET ITEMS (this ISO week, no verdict yet):
{packet_block}

GOVERNOR-ROUTED MOMENTS (calibration_finding / capacity_breach /
life_event_checkpoint / profile_drift — these no longer deliver as standalone
pings; they feed sections 4/5 of THIS brief):
{routed_block}

PRIOR OWNER DECISION CANDIDATE (advice preceded it, no process reflection yet):
{prior_block}

OPEN RESEARCH PROPOSALS:
{proposals_block}

{active_block}

{inputs.anchors_block}

VALIDATION CONSTRAINTS (a violation forces a deterministic fallback):
- Produce exactly five sections: what_changed (a list), highest_priority_decision,
  capital_use, assumption_challenge, decision_revisit (each a single item or null).
- Every item's disposition is exactly one of: action_requested, context_only,
  blocked_missing_evidence, no_action_warranted.
- At most {MAX_NORMAL_WEEK_ACTIONS} action_requested items TOTAL across all
  five sections UNLESS this is an active week AND you populate
  active_week_explanation with a genuine reason (never invent an active week —
  is_active_week is set for you above; do not override it).
- assumption_challenge and decision_revisit should draw from the GOVERNOR-ROUTED
  MOMENTS and PRIOR OWNER DECISION CANDIDATE blocks above when populated — never
  invent a behavioral pattern or a prior decision not present in those blocks.
- Every action_requested item MUST carry an effort_estimate (quick/moderate/
  substantial); non-action items should leave it null.
- NEVER state a numeric probability ("62% chance") — use qualitative confidence
  language instead (house style: "This is my read because...").
- source_refs on every item may ONLY cite the bracketed [ref] tokens shown
  above (e.g. "risk_snapshot:2026-07-20T09:00:00") — never an invented citation.
- Advice, never an executed decision or institutional-process voice.
{corrective_block}

Respond with ONE JSON object matching this shape exactly:
{{
  "what_changed": [{{"title": "...", "body": "...", "disposition": "...",
                      "effort_estimate": "quick"|"moderate"|"substantial"|null,
                      "ticker": "..."|null, "source_refs": ["..."]}}, ...],
  "highest_priority_decision": <same item shape, or null>,
  "capital_use": <same item shape, or null>,
  "assumption_challenge": <same item shape, or null>,
  "decision_revisit": <same item shape, or null>,
  "active_week_explanation": "..."
}}"""


# --------------------------------------------------------------------------- #
# Deterministic fallback
# --------------------------------------------------------------------------- #


def _deterministic_brief(
    inputs: _Inputs, *, extra_degraded: str | None = None
) -> SeniorPartnerBrief:
    """§9.1's mechanical fallback: a labeled digest of artifact POINTERS, no
    synthesized confidence, no LLM prose. Every item is 'context_only' —
    never 'action_requested' without a governed judgment behind it, which
    trivially satisfies the <=3 cap regardless of active-week status."""
    degraded_note = f" ({extra_degraded})" if extra_degraded else ""
    what_changed: list[dict[str, object]] = []
    if inputs.recommendation.line:
        what_changed.append(
            {
                "title": "Incremental Dollar Recommendation on file",
                "body": f"mechanical digest{degraded_note} — no LLM synthesis applied.",
                "disposition": "context_only",
                "effort_estimate": None,
                "ticker": None,
                "source_refs": [inputs.recommendation.ref] if inputs.recommendation.ref else [],
            }
        )
    if inputs.risk.line:
        what_changed.append(
            {
                "title": "Risk Budget snapshot on file",
                "body": f"mechanical digest{degraded_note} — no LLM synthesis applied.",
                "disposition": "context_only",
                "effort_estimate": None,
                "ticker": None,
                "source_refs": [inputs.risk.ref] if inputs.risk.ref else [],
            }
        )
    for line, ref in zip(inputs.cards.lines, inputs.cards.refs, strict=False):
        what_changed.append(
            {
                "title": f"Investment Decision Card pointer: {ref}",
                "body": f"{line} — mechanical digest{degraded_note}, no LLM synthesis applied.",
                "disposition": "context_only",
                "effort_estimate": None,
                "ticker": None,
                "source_refs": [ref],
            }
        )
    for m in inputs.routed:
        what_changed.append(
            {
                "title": f"Routed governor moment: {m.class_}",
                "body": f"{m.line} — mechanical digest{degraded_note}, no LLM synthesis applied.",
                "disposition": "context_only",
                "effort_estimate": None,
                "ticker": m.ticker,
                "source_refs": [m.ref],
            }
        )
    if not what_changed:
        what_changed.append(
            {
                "title": "No inputs available",
                "body": f"mechanical digest{degraded_note} — nothing was on file to summarize.",
                "disposition": "no_action_warranted",
                "effort_estimate": None,
                "ticker": None,
                "source_refs": [],
            }
        )

    decision_revisit = None
    if inputs.prior_decision.line:
        decision_revisit = {
            "title": "Prior decision awaiting reflection",
            "body": f"{inputs.prior_decision.line} — mechanical digest{degraded_note}.",
            "disposition": "context_only",
            "effort_estimate": None,
            "ticker": None,
            "source_refs": [inputs.prior_decision.ref] if inputs.prior_decision.ref else [],
        }

    payload: dict[str, object] = {
        "as_of": inputs.as_of,
        "iso_year": inputs.iso_year,
        "iso_week": inputs.iso_week,
        "input_sha": inputs.input_sha,
        "engine_version": ENGINE_VERSION,
        "prompt_version": prompt_version_for(PURPOSE),
        "selection_mode": "deterministic_fallback",
        "what_changed": what_changed,
        "highest_priority_decision": None,
        "capital_use": None,
        "assumption_challenge": None,
        "decision_revisit": decision_revisit,
        "is_active_week": inputs.is_active_week,
        "active_week_reasons": inputs.active_week_reasons,
        "active_week_explanation": "",
        "source_refs": [],
        "routed_ping_ids": [m.ping_id for m in inputs.routed],
    }
    return SeniorPartnerBrief.model_validate(payload)


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #


def render_markdown(brief: SeniorPartnerBrief) -> str:
    def _fmt_item(item: BriefItem) -> list[str]:
        eff = f" (effort: {item.effort_estimate})" if item.effort_estimate else ""
        return [f"**{item.title}** [{item.disposition}]{eff}", item.body, ""]

    lines = [
        f"# Senior Partner Brief — {brief.iso_year}-W{brief.iso_week:02d} ({brief.as_of})",
        "",
        f"**Mode:** {brief.selection_mode}  |  **Active week:** {brief.is_active_week}",
        "",
        "## 1. What changed that matters",
    ]
    if brief.what_changed:
        for item in brief.what_changed:
            lines += _fmt_item(item)
    else:
        lines.append("(nothing material this week)")
    lines.append("## 2. Highest-priority portfolio decision")
    lines += (
        _fmt_item(brief.highest_priority_decision)
        if brief.highest_priority_decision
        else ["(no single decision rises above the rest this week)"]
    )
    lines.append("## 3. Best current use of incremental capital")
    lines += (
        _fmt_item(brief.capital_use) if brief.capital_use else ["(no capital-use call this week)"]
    )
    lines.append("## 4. An assumption or behavioral pattern worth challenging")
    lines += (
        _fmt_item(brief.assumption_challenge)
        if brief.assumption_challenge
        else ["(nothing surfaced this week)"]
    )
    lines.append("## 5. A prior Owner Decision worth revisiting")
    lines += (
        _fmt_item(brief.decision_revisit)
        if brief.decision_revisit
        else ["(nothing surfaced this week)"]
    )
    if brief.is_active_week and brief.active_week_explanation:
        lines += ["## Why this week is busier", brief.active_week_explanation]
    return "\n".join(lines)


def _call_and_validate(
    inputs: _Inputs, *, db_path: Path, corrective_reasons: list[str] | None = None
) -> tuple[SeniorPartnerBrief, list[str]]:
    prompt = _build_prompt(inputs, corrective_reasons=corrective_reasons)
    payload = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        scope="portfolio",
        expect="object",
        required_keys=("what_changed",),
        db_path=db_path,
    )
    raw = cast("dict[str, object]", payload)
    raw["as_of"] = inputs.as_of
    raw["iso_year"] = inputs.iso_year
    raw["iso_week"] = inputs.iso_week
    raw["input_sha"] = inputs.input_sha
    raw["engine_version"] = ENGINE_VERSION
    raw["prompt_version"] = prompt_version_for(PURPOSE)
    raw.setdefault("selection_mode", "llm")
    raw["is_active_week"] = inputs.is_active_week
    raw["active_week_reasons"] = inputs.active_week_reasons
    raw["routed_ping_ids"] = [m.ping_id for m in inputs.routed]
    try:
        brief = SeniorPartnerBrief.model_validate(raw)
    except Exception as exc:
        raise StructuredParseError(
            f"{PURPOSE}: LLM JSON failed schema validation: {exc}", raw_head=str(raw)[:500]
        ) from exc
    reasons = brief.validate_notification_policy()
    reasons += brief.validate_grounding(allowed_refs=inputs.allowed_refs)
    return brief, reasons


def _persist(
    brief: SeniorPartnerBrief, inputs: _Inputs, *, db_path: Path
) -> tuple[int | None, bool]:
    return llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=None,
            scope="portfolio",
            purpose=PURPOSE,
            content_json=brief.model_dump(mode="json"),
            content_md=render_markdown(brief),
            prompt_version=brief.prompt_version,
            cache_inputs=cast("list[bytes | str]", inputs.cache_inputs),
        ),
        db_path=db_path,
    )


def compose_brief(db_path: Path, repo_root: Path, *, now: datetime | None = None) -> BriefResult:
    """The full P2.2 governed pipeline for one ISO week. Never raises except
    for a genuine hard stop (auth/setup) — every other failure mode degrades
    to a labeled deterministic fallback. Idempotent per ISO-week: a same-week
    re-run with unchanged gathered inputs is a cache hit (no re-spend, no
    duplicate governor drain)."""
    stamp = (now or datetime.now(UTC)).replace(tzinfo=None)
    inputs = _gather_inputs(db_path, repo_root, now=stamp)
    degraded: list[str] = []

    try:
        brief, reasons = _call_and_validate(inputs, db_path=db_path)
        if reasons:
            brief, reasons = _call_and_validate(inputs, db_path=db_path, corrective_reasons=reasons)
        if reasons:
            degraded.append(f"LLM output rejected: {'; '.join(reasons[:5])}")
            brief = _deterministic_brief(
                inputs, extra_degraded="LLM output failed grounding/policy validation twice"
            )
            mode: Literal["llm", "deterministic_fallback"] = "deterministic_fallback"
        else:
            mode = "llm"
    except LLMBudgetExceeded as exc:
        degraded.append(f"budget exceeded: {exc}")
        brief = _deterministic_brief(inputs, extra_degraded="budget exceeded")
        mode = "deterministic_fallback"
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        degraded.append(f"transient LLM failure: {exc}")
        brief = _deterministic_brief(inputs, extra_degraded="transient LLM failure")
        mode = "deterministic_fallback"

    artifact_id, cache_hit = _persist(brief, inputs, db_path=db_path)
    if not cache_hit and inputs.routed:
        # Only drain on a NEW artifact (never on a cache hit — a cache hit
        # means nothing changed since the routed rows were already gathered
        # into the CURRENT artifact, which either already drained them on a
        # prior run or is about to for the first time; a cache hit with
        # undrained rows still present means a prior run's drain failed
        # partway, so draining again here is the correct self-heal).
        try:
            from research.governor import mark_pings_briefed

            mark_pings_briefed([m.ping_id for m in inputs.routed], db_path=db_path)
        except Exception as exc:  # best-effort — a drain failure never fails compose
            degraded.append(f"governor drain failed: {exc}")

    return BriefResult(
        artifact_id=artifact_id,
        brief=brief,
        cache_hit=cache_hit,
        selection_mode=mode,
        degraded_reasons=tuple(degraded),
    )


# --------------------------------------------------------------------------- #
# Telegram surface (PRD §9.1 Telegram / §11.4 privacy)
# --------------------------------------------------------------------------- #

# Any "$1,234.56"-shaped substring — the deterministic redaction net over
# item bodies before they ever reach Telegram. PRD §11.4: Telegram omits
# total portfolio value, exact account balances, and tax-lot detail by
# default; this runs regardless of what the LLM wrote (it saw wealth-context
# dollar figures in its own prompt context), so a prompt-instruction miss can
# never leak a total onto the wire.
_DOLLAR_AMOUNT_RX = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")


def _redact_dollar_amounts(text: str) -> str:
    return _DOLLAR_AMOUNT_RX.sub("[amount omitted]", text)


def _telegram_item_block(label: str, item: BriefItem | None) -> str | None:
    if item is None:
        return None
    ticker_note = f" ({item.ticker})" if item.ticker else ""
    eff = f" · effort: {item.effort_estimate}" if item.effort_estimate else ""
    body = _redact_dollar_amounts(item.body)[:320]
    return f"*{label}*{ticker_note} [{item.disposition}]{eff}\n{item.title}\n{body}"


def build_telegram_text(brief: SeniorPartnerBrief) -> str:
    """PRD §9.1 Telegram surface / §11.4 privacy: ticker, action, recommended
    percentages, rationale, verbal confidence, uncertainty, disconfirmers,
    and a deep link only — NEVER total portfolio value, exact account
    balances, tax-lot detail, or household income/capacity amounts. Every
    item body is passed through :func:`_redact_dollar_amounts` as a
    deterministic safety net beyond the prompt's own instruction."""
    lines = [f"*Senior Partner Brief* — {brief.iso_year}-W{brief.iso_week:02d}"]
    for label, item in (
        ("Highest-priority decision", brief.highest_priority_decision),
        ("Best use of incremental capital", brief.capital_use),
        ("Worth challenging", brief.assumption_challenge),
        ("Worth revisiting", brief.decision_revisit),
    ):
        block = _telegram_item_block(label, item)
        if block:
            lines.append(block)
    if brief.what_changed:
        lines.append("*What changed:*")
        for item in brief.what_changed[:5]:
            lines.append(f"- {_redact_dollar_amounts(item.title)}")
    if brief.is_active_week and brief.active_week_explanation:
        lines.append(
            "_Busier this week:_ " + _redact_dollar_amounts(brief.active_week_explanation)[:240]
        )
    lines.append("Full detail: /mobile/inbox")
    return "\n\n".join(lines)


def private_mobile_inbox_url(explicit: str | None = None) -> str | None:
    """Return the private, phone-reachable Inbox URL.

    ``/mobile/inbox`` by itself is not actionable inside Telegram. The
    configured base must therefore be an absolute HTTP(S) URL (normally the
    Tailscale Serve HTTPS origin). ``explicit`` is primarily for tests and
    one-shot callers. Production first reads
    ``EARNINGS_SUMMARY_PRIVATE_BASE_URL``, then the local
    the external ``private_mobile_base_url`` service configuration. The file
    fallback matters for Windows service deployments such as ``es-poller``:
    service accounts do not inherit the interactive user's environment.
    Interactive scheduled tasks may use their own user-scoped environment.
    """
    from server_runtime.access import private_mobile_origin

    origin = private_mobile_origin(explicit=explicit, config_path=_PRIVATE_BASE_URL_PATH)
    return f"{origin}/mobile/inbox" if origin else None


def _validated_mobile_inbox_link(candidate: str | None) -> str | None:
    """Accept only the canonical Inbox endpoint on a valid private origin."""
    if candidate is None:
        return private_mobile_inbox_url()
    value = candidate.strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.path != "/mobile/inbox" or parsed.params or parsed.query or parsed.fragment:
        return None
    from server_runtime.access import private_mobile_origin

    origin = private_mobile_origin(
        explicit=f"{parsed.scheme}://{parsed.netloc}",
        config_path=_PRIVATE_BASE_URL_PATH,
    )
    return f"{origin}/mobile/inbox" if origin else None


def record_brief_action(
    verb: Literal["defer", "dismiss"],
    *,
    artifact_id: int | None,
    db_path: Path | str | None = None,
) -> bool:
    """Persist one idempotent brief-level owner action.

    Delivery receipts stay untouched so a dismiss cannot accidentally make
    the weekly sender think the brief was never delivered. Actions use their
    own ``standup_messages`` signal kind and signature.
    """
    from user_state._db import now_naive_utc, open_conn

    conn = open_conn(db_path)
    try:
        resolved_id = artifact_id
        if resolved_id is None:
            row = conn.execute(
                "SELECT id FROM llm_artifacts "
                "WHERE purpose = ? AND scope = 'portfolio' "
                "AND superseded_by_id IS NULL ORDER BY generated_at DESC LIMIT 1",
                (PURPOSE,),
            ).fetchone()
            resolved_id = int(row[0]) if row is not None else None
        if resolved_id is None:
            return False
        artifact = conn.execute(
            "SELECT 1 FROM llm_artifacts WHERE id = ? AND purpose = ?",
            (resolved_id, PURPOSE),
        ).fetchone()
        if artifact is None:
            return False
        signature = hashlib.sha256(
            f"senior_partner_brief_action:{resolved_id}:{verb}".encode()
        ).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO standup_messages "
            "(user_id, ticker, signal_kind, signature_sha, status, headline, "
            "evidence_json, created_at) VALUES "
            "('bhanu', NULL, 'senior_partner_brief_action', ?, 'acted', ?, ?, ?)",
            (
                signature,
                f"Senior Partner Brief {verb}",
                json.dumps({"artifact_id": resolved_id, "action": verb}),
                now_naive_utc().isoformat(),
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def dismiss_routed_moment(
    ping_id: int, *, db_path: Path | str | None = None
) -> tuple[bool, str | None]:
    """The ONE action core for dismissing a single governor-routed moment
    (calibration_finding / capacity_breach / life_event_checkpoint /
    profile_drift — the four ``BRIEF_ROUTED_CLASSES``) FROM the brief —
    reachable identically from the desktop Today card, the mobile Inbox, and
    a Telegram ``spb:dismiss_item:<ping_id>`` callback. A thin wrapper over
    ``research.governor.record_dismissal`` so every surface shares the SAME
    mute-learning path the pre-P2.2 standalone pings always had: three
    consecutive dismissals of a class mute it (owner control, not merely a
    UI affordance — the brief-routed classes must stay just as mutable as
    the classes that still send directly).

    This is distinct from the brief-LEVEL 'Dismiss' action (Telegram
    ``spb:dismiss``): dismissing the WHOLE brief is bookkeeping on the brief
    itself and never touches an individual ``coach_pings`` row or the mute
    ledger — only a per-item dismiss (this function) counts toward
    MUTE_AFTER.

    Returns ``(recorded, muted_class)`` — see ``record_dismissal``."""
    from research.governor import record_dismissal

    return record_dismissal(ping_id, db_path=db_path)


def build_telegram_keyboard(
    brief: SeniorPartnerBrief,
    *,
    artifact_id: int | None = None,
    inbox_url: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, object]:
    """Why / Review in Inbox / Defer / Dismiss (brief-level) — the
    weekly-packet button pattern (``pipeline.weekly_packet.item_keyboard``),
    keyed to the brief rather than a single packet item id — PLUS one
    per-item 'Dismiss <class>' row for every governor-routed moment this
    brief drained (``brief.routed_ping_ids``), each wired to
    ``spb:dismiss_item:<ping_id>`` -> :func:`dismiss_routed_moment`. The
    brief-level Dismiss button is UNCHANGED: it never mutes anything.
    ``db_path`` is optional (only used to resolve a friendly class label per
    ping — every pre-existing caller that omits it still gets a working,
    if generically-labeled, keyboard)."""
    suffix = f":{artifact_id}" if artifact_id is not None else ""
    resolved_inbox_url = _validated_mobile_inbox_link(inbox_url)
    if resolved_inbox_url is None:
        raise ValueError("a validated private mobile Inbox URL is required for Telegram delivery")
    review_button: dict[str, object] = {
        "text": "Review in Inbox",
        "url": resolved_inbox_url,
    }
    rows: list[list[dict[str, object]]] = [
        [
            {"text": "Why?", "callback_data": f"spb:why{suffix}"},
            review_button,
        ],
        [
            {"text": "Defer", "callback_data": f"spb:defer{suffix}"},
            {"text": "Dismiss", "callback_data": f"spb:dismiss{suffix}"},
        ],
    ]
    if brief.routed_ping_ids:
        labels: dict[int, str] = {}
        if db_path is not None:
            from research.governor import get_ping

            for pid in brief.routed_ping_ids:
                row = get_ping(pid, db_path=db_path)
                if row is not None:
                    labels[pid] = row.class_.replace("_", " ")
        for pid in brief.routed_ping_ids:
            label = labels.get(pid, f"item #{pid}")
            rows.append([{"text": f"Dismiss {label}", "callback_data": f"spb:dismiss_item:{pid}"}])
    return {"inline_keyboard": rows}

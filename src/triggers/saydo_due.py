"""SayDo-due sensor — full implementation (PR-N12).

``Cadence.CALENDAR_DRIVEN`` sensor over ``management_commitments`` (alembic
0017). A management commitment carries a ``period_target`` — the quarter
management was guiding for — plus the KPI, comparator, and target value. Once
that period arrives *and* a realized value is on file, the commitment is
gradeable: did management keep its word? This sensor surfaces each such
verdict exactly once, the moment it comes due.

This is the slice closest to a "Management Promise & Delivery Ledger": it turns
the say/do record into actionable alerts (follow up on the call, append to the
bear case when management missed, update the thesis with the verdict).

CRITICAL contract (mirrors ``kpi_inflection``, the inverse of ``earnings_tone``)
-------------------------------------------------------------------------------
The verdict is **deterministic** — it is either read from the row's ``outcome``
column or computed in code from ``comparator``/``target_value``/
``realized_value``. So the alert MUST fire even when the LLM is down.
``build_alert`` composes a factual memo from the evidence first; the optional
1-2 sentence "what it means for the thesis" line is best-effort enrichment that
degrades silently to no-context on any failure. The alert fires regardless.

Self-contained grading
-----------------------
The trigger does NOT depend on the heavy SayDo grading pipeline
(``build_saydo_pairs`` / batch submission) having run. When ``outcome`` is
already populated (the matcher ran) it is read; when it is absent the verdict is
computed inline from the comparator arithmetic. Either path yields the same
normalized verdict vocabulary (MET / MISSED / MIXED). A commitment with no
``realized_value`` is not gradeable → skipped (no fire).

Recency window
--------------
Without a window the first run would flood with every historical verdict. The
driver's ``signature_sha`` dedup prevents *re-firing*, but only the recency
window (``_DUE_RECENCY_DAYS``) prevents the initial flood: a commitment is
surfaced only when its ``period_target`` arrived within roughly the last
quarter.

Path bridging
-------------
``scan`` is handed a ``sqlite3.Connection`` (the driver opens one per
(ticker, trigger) pair), but the canonical reader ``load_saydo_verdicts`` takes
a ``db_path``. We resolve the file backing the connection via
``PRAGMA database_list`` so the read hits the *exact* DB the driver opened.
``build_alert`` is not handed a connection at all, so — like ``kpi_inflection``
— it resolves ``db.DB_PATH`` for the artifact-store cache and the repo root for
the thesis anchor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from alerts.store import compute_signature_sha
from llm.anchors import (
    compose_anchor_block,
    load_bear_anchor,
    load_ir_anchor,
    load_thesis_anchor,
)
from llm_artifact_store import (
    UpsertRequest,
    compute_input_sha256,
    read_current,
    upsert,
)
from llm_client import call_llm
from report.sections.p3_data import SayDoVerdictRow, load_saydo_verdicts
from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    TriggerCandidate,
    UserStateContext,
)

log = logging.getLogger(__name__)

# A commitment is "due" when period_target <= now, and surfaced only when that
# target arrived within the last _DUE_RECENCY_DAYS (~ one quarter). The window
# stops the first run from flooding with every historical verdict; the
# signature_sha dedup handles re-fires on subsequent runs.
_DUE_RECENCY_DAYS = 100

# Generous read cap so prior-miss counting reflects the ticker's full record,
# not just the most-recent handful of commitments.
_HISTORY_LIMIT = 500


class Verdict(StrEnum):
    """Normalized say/do verdict — the single vocabulary the alert speaks.

    The ``management_commitments.outcome`` column is free-form TEXT and, in the
    wild, carries two vocabularies: the matcher (``compute.say_do``) writes
    ``hit``/``miss``/``beat``/``no_data``; other writers use ``met``/``missed``.
    Both — and the inline-computed verdict — normalize onto these three values
    so the memo, evidence, and downstream actions never have to branch on the
    raw string.
    """

    MET = "MET"
    MISSED = "MISSED"
    MIXED = "MIXED"


# Raw outcome strings that normalize onto each verdict. Mirrors
# compute.say_do.CommitmentOutcome (hit/miss/beat/no_data) plus the alternate
# met/missed/mixed/partial vocabulary seen in some rows. Exact-token membership
# (not substring matching) on a lowercased value.
_MET_OUTCOMES: frozenset[str] = frozenset({"hit", "beat", "met", "in_line", "inline"})
_MISSED_OUTCOMES: frozenset[str] = frozenset({"miss", "missed"})
_MIXED_OUTCOMES: frozenset[str] = frozenset({"mixed", "partial"})

# Comparator vocabulary. Mirrors compute.thesis_evaluator.Comparator value
# strings (lt/le/gt/ge/eq) plus the symbol forms (<, <=, >, >=, =, ~) that
# appear in some management_commitments rows.
_CMP_GE: frozenset[str] = frozenset({"ge", "gte", ">="})
_CMP_GT: frozenset[str] = frozenset({"gt", ">"})
_CMP_LE: frozenset[str] = frozenset({"le", "lte", "<="})
_CMP_LT: frozenset[str] = frozenset({"lt", "<"})
_CMP_EQ: frozenset[str] = frozenset({"eq", "=", "==", "~", "approx", "about"})

# EQ ("deliver ~X") tolerance bands: within 5% of target = MET; 5-10% off =
# MIXED (near-miss / partial credit); beyond 10% = MISSED. Directional
# comparators (ge/gt/le/lt) are strict pass/fail and never yield MIXED.
_EQ_MET_TOLERANCE = 0.05
_EQ_MIXED_TOLERANCE = 0.10

# Verdicts worth firing on. MET is thesis-confirming, MISSED/MIXED are
# thesis-challenging — all three are surfaced.
_FIREABLE: frozenset[str] = frozenset(
    {Verdict.MET.value, Verdict.MISSED.value, Verdict.MIXED.value}
)

# Verdicts that warrant a bear-case append (management did not cleanly deliver).
_ADVERSE: frozenset[str] = frozenset({Verdict.MISSED.value, Verdict.MIXED.value})

# Units rendered with no separating space / bare.
_UNITLESS_UNITS: frozenset[str] = frozenset({"", "actual", "number", "count", "x", "ratio"})
_PERCENT_UNITS: frozenset[str] = frozenset({"%", "percent", "pct", "percentage"})

# queued_actions.action_kind vocabulary (see triggers.base.QueuedActionDraft).
_ACTION_EARNINGS_PREP = "earnings_prep_append"
_ACTION_BEAR_APPEND = "bear_append"
_ACTION_THESIS_UPDATE = "thesis_update"

# Artifact-store purpose for the optional LLM context line. Must match the entry
# in llm_artifact_store.FACT_DEPENDENT_PURPOSES so a fact-side restatement marks
# the cached context dirty via the existing chain.
_ARTIFACT_PURPOSE = "saydo_due_context"
# Prompt-version key for the cache. Bump when the context prompt body changes
# materially.
_PROMPT_VERSION = "v1"


# --------------------------------------------------------------------------
# Verdict normalization / computation
# --------------------------------------------------------------------------


def _normalize_outcome(raw: str) -> Verdict | None:
    """Map a raw ``outcome`` string onto a :class:`Verdict`, or None when the
    string is unrecognized or signals ungradeable (e.g. ``no_data``)."""
    token = raw.strip().lower()
    if token in _MET_OUTCOMES:
        return Verdict.MET
    if token in _MISSED_OUTCOMES:
        return Verdict.MISSED
    if token in _MIXED_OUTCOMES:
        return Verdict.MIXED
    return None


def _compute_verdict(comparator: str, target: float, realized: float) -> Verdict | None:
    """Deterministic verdict from the comparator arithmetic, or None when the
    comparator is unrecognized (can't grade numerically).

    Directional comparators are strict pass/fail; EQ uses a tolerance band so a
    near-miss on a "deliver ~X" commitment grades MIXED rather than MISSED.
    """
    cmp = comparator.strip().lower()
    if cmp in _CMP_GE:
        return Verdict.MET if realized >= target else Verdict.MISSED
    if cmp in _CMP_GT:
        return Verdict.MET if realized > target else Verdict.MISSED
    if cmp in _CMP_LE:
        return Verdict.MET if realized <= target else Verdict.MISSED
    if cmp in _CMP_LT:
        return Verdict.MET if realized < target else Verdict.MISSED
    if cmp in _CMP_EQ:
        denom = abs(target) if target != 0 else 1.0
        deviation = abs(realized - target) / denom
        if deviation <= _EQ_MET_TOLERANCE:
            return Verdict.MET
        if deviation <= _EQ_MIXED_TOLERANCE:
            return Verdict.MIXED
        return Verdict.MISSED
    return None


def _resolve_verdict(row: SayDoVerdictRow) -> Verdict | None:
    """The graded verdict for a commitment, or None when it isn't gradeable.

    Prefers the persisted ``outcome`` (the matcher's authority) when it is
    populated and recognized; otherwise computes inline from the comparator
    arithmetic. Returns None when there is no realized value, or when neither
    the outcome string nor the comparator can be interpreted.
    """
    if row.realized_value is None:
        return None
    if row.outcome is not None and row.outcome.strip():
        read = _normalize_outcome(row.outcome)
        if read is not None:
            return read
    return _compute_verdict(row.comparator, row.target_value, row.realized_value)


# --------------------------------------------------------------------------
# Pure formatting helpers
# --------------------------------------------------------------------------


def _format_value(value: float, unit: str | None) -> str:
    """Render a numeric value with its unit suffix. '%' attaches with no space;
    other word units attach with a leading space; unitless / sentinel units
    render bare. ``%g`` trims trailing zeros (16.40 → 16.4)."""
    num = f"{value:g}"
    if unit is None:
        return num
    u = unit.strip()
    lowered = u.lower()
    if lowered in _UNITLESS_UNITS:
        return num
    if lowered in _PERCENT_UNITS:
        return f"{num}%"
    return f"{num} {u}"


def _comparator_symbol(comparator: str) -> str:
    """Human-readable comparator for the memo (ge → '>='). Unknown → raw."""
    cmp = comparator.strip().lower()
    if cmp in _CMP_GE:
        return ">="
    if cmp in _CMP_GT:
        return ">"
    if cmp in _CMP_LE:
        return "<="
    if cmp in _CMP_LT:
        return "<"
    if cmp in _CMP_EQ:
        return "~"
    return comparator


def _ordinal(n: int) -> str:
    """1 → '1st', 2 → '2nd', 3 → '3rd', 4 → '4th', 11 → '11th'."""
    teens = 10 <= n % 100 <= 20
    suffix = "th" if teens else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _as_float(value: object) -> float:
    """Narrow a numeric evidence value to float. Fails loud on a non-numeric
    (rejecting bool) — scan() always stores target/realized as floats, so a miss
    here is a contract bug, not user input to absorb."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise TypeError(f"expected numeric evidence value, got {type(value).__name__}")


def _sha256_text(text: str) -> str:
    """Deterministic sha256 over UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _factual_memo(ticker: str, evidence: Mapping[str, object]) -> str:
    """Deterministic one-line memo built entirely from the commitment evidence.

    This is the contract: composed with no LLM involvement so the alert has a
    body even when the enrichment call fails.
    """
    kpi_name = str(evidence["kpi_name"])
    period_made = str(evidence["period_made"])
    period_target = str(evidence["period_target"])
    comparator = str(evidence["comparator"])
    narrative = str(evidence["narrative"])
    outcome = str(evidence["outcome"])
    unit_raw = evidence.get("unit")
    unit = unit_raw if isinstance(unit_raw, str) else None
    target_str = _format_value(_as_float(evidence["target_value"]), unit)
    realized_str = _format_value(_as_float(evidence["realized_value"]), unit)

    memo = (
        f"{ticker}'s {period_made} commitment — '{narrative}' "
        f"(target: {kpi_name} {_comparator_symbol(comparator)} {target_str} "
        f"by {period_target}) — is due. "
        f"Verdict: {outcome} (realized: {realized_str})."
    )

    prior = evidence.get("prior_miss_count")
    if (
        isinstance(prior, int)
        and not isinstance(prior, bool)
        and prior >= 1
        and outcome == Verdict.MISSED.value
    ):
        memo += f" This is the {_ordinal(prior + 1)} miss for {ticker} on record."
    return memo


def _build_context_prompt(factual_core: str, anchor_block: str) -> str:
    """Short prompt for the optional 'what it means for the thesis' line."""
    anchor = anchor_block.strip() or "(no thesis anchor on file)"
    return (
        f"A management commitment just came due:\n{factual_core}\n\n"
        f"{anchor}\n\n"
        "In 1-2 sentences, explain what this say/do verdict means for the "
        "thesis — whether it confirms or challenges management's credibility. "
        "Plain text, no preamble, no markdown."
    )


def _context_cache_inputs(
    *,
    ticker: str,
    kpi_name: str,
    period_target: str,
    factual_core: str,
    anchor_text: str,
) -> list[bytes | str]:
    """Deterministic cache-input list for ``compute_input_sha256``. Order is
    part of the contract — changing it invalidates every cached context."""
    return [
        f"ticker={ticker}",
        f"kpi={kpi_name}",
        f"period_target={period_target}",
        "factual_sha=" + _sha256_text(factual_core),
        "anchor_sha=" + _sha256_text(anchor_text),
    ]


# --------------------------------------------------------------------------
# DB path / repo root helpers (Protocol passes no connection to build_alert;
# mirrors kpi_inflection).
# --------------------------------------------------------------------------


def _conn_db_path(conn: sqlite3.Connection) -> Path | None:
    """File path backing the 'main' schema of an open connection, or None for an
    in-memory / unbacked connection or on any sqlite error."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # (seq, name, file) — 'main' is the primary attached DB.
        if len(row) >= 3 and row[1] == "main":
            file_path = row[2]
            if isinstance(file_path, str) and file_path:
                return Path(file_path)
    return None


def _repo_root() -> Path:
    """``src/triggers/saydo_due.py`` → parents[2] = project root. Used to address
    ``micro_thesis/holdings/<TICKER>.json`` via load_thesis_anchor."""
    return Path(__file__).resolve().parents[2]


def _db_path() -> Path:
    """Resolve the portfolio DB for the artifact-store cache. Late import so a
    test's ``monkeypatch.setattr('db.DB_PATH', ...)`` is honored."""
    from db import DB_PATH

    return Path(DB_PATH)


# --------------------------------------------------------------------------
# Sensor implementation
# --------------------------------------------------------------------------


class SayDoDueTrigger:
    """Calendar-driven sensor over ``management_commitments`` — surfaces a
    say/do verdict the moment its target period comes due and it is gradeable."""

    kind: ClassVar[str] = "saydo_due"
    cadence: ClassVar[Cadence] = Cadence.CALENDAR_DRIVEN

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]:
        """Emit one candidate per commitment that is now due, recent, and
        gradeable. Returns ``[]`` when the table is missing, the DB path can't be
        resolved, or nothing qualifies — never raises."""
        db_path = _conn_db_path(db)
        if db_path is None:
            log.debug({"event": "saydo_due_scan_no_db_path", "ticker": ticker})
            return []

        rows = load_saydo_verdicts(ticker, db_path=db_path, limit=_HISTORY_LIMIT)
        if not rows:
            return []

        # Verdict for every row that has a realized value, used both to grade the
        # due commitments and to count a ticker's prior misses.
        graded: list[tuple[datetime, Verdict]] = []
        for row in rows:
            verdict = _resolve_verdict(row)
            if verdict is not None:
                graded.append((row.period_target, verdict))

        now = datetime.now(UTC).replace(tzinfo=None)
        recency_floor = now - timedelta(days=_DUE_RECENCY_DAYS)
        candidates: list[TriggerCandidate] = []
        for row in rows:
            candidate = self._candidate_from_row(
                ticker=ticker,
                row=row,
                graded=graded,
                now=now,
                recency_floor=recency_floor,
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _candidate_from_row(
        self,
        *,
        ticker: str,
        row: SayDoVerdictRow,
        graded: list[tuple[datetime, Verdict]],
        now: datetime,
        recency_floor: datetime,
    ) -> TriggerCandidate | None:
        """Build a candidate iff ``row`` is due (target arrived), within the
        recency window, gradeable (has a realized value + resolvable verdict)."""
        if not (recency_floor <= row.period_target <= now):
            return None  # not due yet, or due too long ago to surface
        if row.realized_value is None:
            return None  # data not in yet — not gradeable
        verdict = _resolve_verdict(row)
        if verdict is None:
            return None  # uninterpretable comparator + outcome — skip

        period_target_iso = row.period_target.date().isoformat()
        period_made_iso = row.period_made.date().isoformat()
        prior_miss_count = sum(
            1 for target, v in graded if v is Verdict.MISSED and target < row.period_target
        )

        evidence: dict[str, object] = {
            "kpi_name": row.kpi_name,
            "period_made": period_made_iso,
            "period_target": period_target_iso,
            "comparator": row.comparator,
            "target_value": row.target_value,
            "realized_value": row.realized_value,
            "unit": row.unit,
            "narrative": row.narrative,
            "outcome": verdict.value,
            "prior_miss_count": prior_miss_count,
        }
        key = f"{ticker}:{row.kpi_name}:{period_target_iso}"
        return TriggerCandidate(
            ticker=ticker,
            kind=self.kind,
            key=key,
            evidence=evidence,
            computed_at=now,
        )

    def should_fire(self, candidate: TriggerCandidate, user_state: UserStateContext) -> bool:
        """Fire on any gradeable due commitment — MET (thesis-confirming),
        MISSED, or MIXED (thesis-challenging). scan() already filtered out the
        ungradeable rows, so this is True for every emitted candidate. Driver-side
        dedup (find_by_signature) owns dismissed-alert suppression, so we don't
        re-check ``recent_dismissed_signatures`` here."""
        _ = user_state
        outcome = candidate.evidence.get("outcome")
        return isinstance(outcome, str) and outcome in _FIREABLE

    def signature_key_evidence(self, candidate: TriggerCandidate) -> Mapping[str, object]:
        """Key the dedup hash on (kpi_name, period_target): a commitment coming
        due fires exactly once. Must match the key_evidence ``build_alert`` feeds
        compute_signature_sha so the driver's pre-build dedup agrees with the
        persisted signature."""
        return {
            "kpi_name": candidate.evidence["kpi_name"],
            "period_target": candidate.evidence["period_target"],
        }

    def build_alert(self, candidate: TriggerCandidate, anchor: ThesisAnchor | None) -> AlertDraft:
        """Assemble the alert. The deterministic factual memo is built first and
        is the contract; the optional LLM context line is appended only when the
        best-effort enrichment succeeds. Never raises on LLM failure."""
        _ = anchor  # driver passes None; the markdown anchor is loaded from disk
        ticker = candidate.ticker
        evidence = candidate.evidence

        factual_memo = _factual_memo(ticker, evidence)
        llm_context = self._maybe_llm_context(
            ticker=ticker, evidence=evidence, factual_core=factual_memo
        )
        memo_text = factual_memo if llm_context is None else f"{factual_memo} {llm_context}"

        evidence_obj = dict(evidence)
        evidence_obj["llm_context_available"] = llm_context is not None
        evidence_json = json.dumps(evidence_obj, sort_keys=True, ensure_ascii=False, default=str)
        signature_sha = compute_signature_sha(
            self.kind, ticker, self.signature_key_evidence(candidate)
        )
        fired_at = datetime.now(UTC).replace(tzinfo=None)

        return AlertDraft(
            trigger_kind=self.kind,
            ticker=ticker,
            fired_at=fired_at,
            evidence_json=evidence_json,
            signature_sha=signature_sha,
            memo_text=memo_text,
        )

    def draft_actions(
        self, alert: AlertDraft, candidate: TriggerCandidate
    ) -> list[QueuedActionDraft]:
        """Propose downstream mutations.

        Always an ``earnings_prep_append`` (follow up on the call) and a
        ``thesis_update`` (record the verdict). When management missed or only
        partially delivered (MISSED / MIXED), add a ``bear_append`` so the
        broken promise lands in the bear case.
        """
        evidence = candidate.evidence
        kpi_name = evidence.get("kpi_name")
        outcome = evidence.get("outcome")
        if not isinstance(kpi_name, str) or not isinstance(outcome, str):
            return []

        period_target = str(evidence.get("period_target"))
        narrative = str(evidence.get("narrative"))
        comparator = _comparator_symbol(str(evidence.get("comparator")))
        unit_raw = evidence.get("unit")
        unit = unit_raw if isinstance(unit_raw, str) else None
        target_str = _format_value(_as_float(evidence["target_value"]), unit)
        realized_str = _format_value(_as_float(evidence["realized_value"]), unit)
        memo_text = alert.memo_text or ""

        actions: list[QueuedActionDraft] = [
            QueuedActionDraft(
                action_kind=_ACTION_EARNINGS_PREP,
                payload={
                    "body": (
                        f"Follow up on the {kpi_name} commitment outcome "
                        f"({outcome}) on the next call."
                    ),
                    "kpi_name": kpi_name,
                },
            )
        ]

        if outcome in _ADVERSE:
            actions.append(
                QueuedActionDraft(
                    action_kind=_ACTION_BEAR_APPEND,
                    payload={
                        "body": (
                            f"Management {outcome.lower()} commitment: "
                            f"'{narrative}'. Realized {realized_str} vs target "
                            f"{comparator} {target_str}."
                        ),
                        "kpi_name": kpi_name,
                    },
                )
            )

        actions.append(
            QueuedActionDraft(
                action_kind=_ACTION_THESIS_UPDATE,
                payload={
                    "body": (
                        f"SayDo verdict on {kpi_name} ({period_target}): {outcome}. {memo_text}"
                    ),
                    "kpi_name": kpi_name,
                    "outcome": outcome,
                },
            )
        )
        return actions

    # ------------------------------------------------------------------
    # Internal — optional LLM enrichment (best-effort, cache-aware)
    # ------------------------------------------------------------------

    def _maybe_llm_context(
        self, *, ticker: str, evidence: Mapping[str, object], factual_core: str
    ) -> str | None:
        """Best-effort 1-2 sentence context line, or None.

        Degrades silently to None on ANY failure (LLM down, budget cap, empty
        response, store error) so build_alert always emits the deterministic
        memo. Cache-aware via the artifact store.
        """
        kpi_name = str(evidence["kpi_name"])
        period_target = str(evidence["period_target"])
        repo_root = _repo_root()
        db_path = _db_path()

        try:
            # Compose the full 3-block anchor (thesis + bear + IR) so the
            # "what it means for the thesis" line can weigh a met/missed
            # commitment against the bear case and management's IR framing;
            # compose_anchor_block omits whichever blocks are absent.
            anchor_block = compose_anchor_block(
                load_thesis_anchor(repo_root, ticker),
                load_bear_anchor(repo_root, ticker),
                load_ir_anchor(repo_root, ticker),
            )
        except Exception as exc:  # anchor is optional; never block the memo
            log.debug(
                {
                    "event": "saydo_due_anchor_load_failed",
                    "ticker": ticker,
                    "error": str(exc),
                }
            )
            anchor_block = ""

        cache_inputs = _context_cache_inputs(
            ticker=ticker,
            kpi_name=kpi_name,
            period_target=period_target,
            factual_core=factual_core,
            anchor_text=anchor_block,
        )
        try:
            return self._read_cached_or_call_llm_context(
                ticker=ticker,
                kpi_name=kpi_name,
                period_target=period_target,
                factual_core=factual_core,
                anchor_block=anchor_block,
                cache_inputs=cache_inputs,
                db_path=db_path,
            )
        except Exception as exc:
            # The deterministic memo is the contract — an LLM/budget/parse failure
            # here must NOT sink the alert. Log and proceed without the context.
            log.warning(
                {
                    "event": "saydo_due_context_failed",
                    "ticker": ticker,
                    "kpi": kpi_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return None

    def _read_cached_or_call_llm_context(
        self,
        *,
        ticker: str,
        kpi_name: str,
        period_target: str,
        factual_core: str,
        anchor_block: str,
        cache_inputs: list[bytes | str],
        db_path: Path,
    ) -> str | None:
        """Return the context line, hitting the artifact-store cache when
        possible. Per-commitment artifacts share the (ticker, purpose) slot, so
        the (kpi_name, period_target) tuple rides in ``fiscal_period`` to keep
        distinct commitments from superseding each other; ``input_sha256`` still
        guards correctness."""
        fiscal_period = f"{kpi_name}:{period_target}"
        existing = read_current(
            ticker=ticker,
            purpose=_ARTIFACT_PURPOSE,
            fiscal_period=fiscal_period,
            db_path=db_path,
        )
        new_sha = compute_input_sha256(prompt_version=_PROMPT_VERSION, cache_inputs=cache_inputs)
        if (
            existing is not None
            and not existing.dirty
            and existing.input_sha256 == new_sha
            and isinstance(existing.content_md, str)
            and existing.content_md.strip()
        ):
            log.info(
                {
                    "event": "saydo_due_context_cache_hit",
                    "ticker": ticker,
                    "kpi": kpi_name,
                    "artifact_id": existing.id,
                }
            )
            return existing.content_md

        prompt = _build_context_prompt(factual_core, anchor_block)
        raw = call_llm(prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker)
        context = raw.strip()
        if not context:
            return None

        _ = upsert(
            UpsertRequest(
                ticker=ticker,
                purpose=_ARTIFACT_PURPOSE,
                fiscal_period=fiscal_period,
                content_md=context,
                cache_inputs=cache_inputs,
                prompt_version=_PROMPT_VERSION,
            ),
            db_path=db_path,
        )
        return context

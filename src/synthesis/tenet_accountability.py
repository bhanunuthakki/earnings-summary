"""Weekly Tenet accountability pass (B5, 2026-07-19 program overhaul).

A Tenet (``synthesis.tenets``) is a durable belief about HOW the owner
invests. Nothing in the platform previously checked whether the owner's OWN
decisions actually lived up to it — a Tenet could sit in the Worldview panel
forever, unfalsified, unconfronted. This module closes that loop: once a
week, for every ``current`` Tenet, it deterministically gathers the owner's
own decisions made since the Tenet's ``as_of`` (plus, best-effort, the
realized position alpha on the tickers involved), then makes ONE structured
LLM call auditing the Tenet against that evidence — which decisions upheld
it, which violated it, and a rough dollar cost of the violations.

Every cited decision id is validated against what was actually shown to the
model (the same grounding discipline as ``synthesis.session_distill`` /
``synthesis.tenet_distill`` — a fabricated citation is dropped, never
trusted). The result is persisted onto the Tenet's OWN ``insight_notes.meta_json``
row (key ``"accountability"``, replaced wholesale each run — this module
keeps only the LATEST verdict; the weekly cron's own log, not this row, is
the history) so:

  * the Worldview panel (``src/pipeline/worldview_panel.py``) can render a
    compact receipts chip ("upheld 4 / violated 2 · ~-$4.2k") on the Tenet's
    card, and
  * the nag governor (``src/research/governor.py``) can raise AT MOST ONE
    ``tenet_challenge`` moment per week — the tenet with the costliest
    violation record — without making any LLM call of its own (it reads the
    persisted verdict deterministically; see that module).

Degrade-safety follows the repo's quota-scheduling contract
(``directives/llm_quota_scheduling.md`` rule 3): a Tenet with no owner
decisions since its ``as_of`` is skipped with ZERO LLM calls (nothing to
judge); a transient LLM failure defers that Tenet — tallied
``deferred_transient``, retried next week's sweep, nothing written — while a
hard stop (budget block / missing CLI) propagates so the runner exits loudly.
One bad Tenet never sinks the whole sweep.

Position alpha is read through the SAME typed tracker client every other
analytics reader uses (``integrations.portfolio_tracker_client``) — this
module never re-derives the HTTP call. The tracker is a Windows logon task
with a real cold-start window; a short explicit timeout plus a
belt-and-braces ``except Exception`` degrade the alpha figure to ``None``
rather than ever blocking the pass — ``assess_tenet`` then judges purely on
process (did the decision follow the Tenet?) instead of P&L.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from clock import now_naive_utc
from synthesis.insights import InsightRow
from synthesis.tenets import list_tenets
from user_state._db import now_iso, open_conn

log = logging.getLogger(__name__)

PURPOSE = "tenet_accountability"

# One structured call per Tenet: the rendered prompt -> a verdict dict, or
# None on "the call itself produced nothing usable". Injected so the engine
# is unit-testable without the CLI (mirrors synthesis.session_distill's
# DistillCall / synthesis.tenet_distill's DistillCall).
AssessCall = Callable[[str], "dict[str, object] | None"]

_MAX_DECISIONS = 30
# Short and deliberately tight: the tracker is a logon task with a real
# cold-start window (~15s to become responsive), and this weekly pass must
# never block on it. A miss degrades alpha to None (see module docstring).
_TRACKER_TIMEOUT_SECONDS = 4.0
_ONE_LINER_MAX_CHARS = 120


@dataclass(frozen=True, slots=True)
class DecisionEvidenceRow:
    """One owner decision shown to the model, cited as ``[D<decision_id>]``."""

    decision_id: int
    ticker: str | None
    kind: str  # recommendation_kind: 'add' | 'trim' | 'hold' | 'sell' | 'initiate' | 'avoid' | ...
    conviction: str | None
    outcome_label: str | None
    outcome_pct: float | None
    made_at: str
    falsifier: str | None


@dataclass(frozen=True, slots=True)
class Evidence:
    """The deterministic, zero-LLM evidence bundle for one Tenet's audit.

    ``alpha_by_ticker`` is ``None`` (not ``{}``) when the tracker couldn't be
    reached at all — distinct from a live tracker that simply has no alpha
    row for a ticker (KeyError on lookup), so ``assess_tenet`` can tell "no
    data available" from "no alpha for this name"."""

    decisions: list[DecisionEvidenceRow]
    alpha_by_ticker: dict[str, float] | None


@dataclass(frozen=True, slots=True)
class Assessment:
    """A validated, grounded verdict for one Tenet — every id in ``upheld``/
    ``violated`` is a real ``decision_id`` that was shown to the model."""

    upheld: list[int]
    violated: list[int]
    est_cost_usd: float | None
    one_liner: str


def _fetch_alpha(tenet: InsightRow, *, db_path: Path | str) -> dict[str, float] | None:
    """Per-ticker dollar alpha since the Tenet's ``as_of``, via the shared
    tracker client. ``None`` on ANY problem — the client's own read
    functions never raise on a tracker/network issue, but a cold-starting
    logon-task tracker is exactly the flaky dependency this belt-and-braces
    ``except`` guards against; a caught exception here must never abort the
    accountability pass, only drop the alpha figure."""
    try:
        from integrations.portfolio_tracker_client import fetch_portfolio_analytics

        analytics = fetch_portfolio_analytics(
            only={"position_alpha"},
            start_date=tenet.as_of[:10],
            timeout=_TRACKER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.debug(
            {
                "event": "tenet_accountability_alpha_unavailable",
                "tenet_id": tenet.id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
    if analytics.position_alpha is None:
        return None
    return {
        row.ticker.upper(): row.alpha
        for row in analytics.position_alpha.rows
        if row.ticker and row.alpha is not None
    }


def gather_evidence(tenet: InsightRow, *, db_path: Path | str) -> Evidence:
    """Deterministic, zero-LLM: the owner's OWN decisions made since the
    Tenet's ``as_of``, newest-first, capped at :data:`_MAX_DECISIONS`, plus
    per-ticker realized alpha over the same window (best-effort; degrades to
    ``None`` — see :func:`_fetch_alpha`)."""
    conn = open_conn(db_path)
    try:
        try:
            rows = conn.execute(
                """
                SELECT decision_id, ticker, recommendation_kind, conviction,
                       outcome_label, outcome_pct, made_at, falsifier
                FROM v_decision_journal
                WHERE decided_by = 'owner' AND made_at >= ?
                ORDER BY made_at DESC, decision_id DESC
                LIMIT ?
                """,
                (tenet.as_of, _MAX_DECISIONS),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []  # pre-0179 schema — no v_decision_journal to read
    finally:
        conn.close()
    decisions = [
        DecisionEvidenceRow(
            decision_id=int(r[0]),
            ticker=str(r[1]) if r[1] is not None else None,
            kind=str(r[2]),
            conviction=str(r[3]) if r[3] is not None else None,
            outcome_label=str(r[4]) if r[4] is not None else None,
            outcome_pct=float(r[5]) if r[5] is not None else None,
            made_at=str(r[6]),
            falsifier=str(r[7]) if r[7] is not None else None,
        )
        for r in rows
    ]
    return Evidence(decisions=decisions, alpha_by_ticker=_fetch_alpha(tenet, db_path=db_path))


def _build_prompt(tenet: InsightRow, evidence: Evidence) -> str:
    lines: list[str] = []
    for d in evidence.decisions:
        alpha_txt = ""
        if evidence.alpha_by_ticker is not None and d.ticker:
            alpha = evidence.alpha_by_ticker.get(d.ticker.upper())
            if alpha is not None:
                alpha_txt = f", position alpha since: ${alpha:,.0f}"
        conv_txt = f", conviction={d.conviction}" if d.conviction else ""
        outcome_txt = ""
        if d.outcome_label:
            outcome_txt = f", outcome={d.outcome_label}"
            if d.outcome_pct is not None:
                outcome_txt += f" ({d.outcome_pct:+.1f}%)"
        falsifier_txt = f' — falsifier: "{d.falsifier}"' if d.falsifier else ""
        lines.append(
            f"[D{d.decision_id}] {d.made_at[:10]} {d.kind.upper()} {d.ticker or 'portfolio'}"
            f"{conv_txt}{outcome_txt}{alpha_txt}{falsifier_txt}"
        )
    decisions_block = "\n".join(lines) or "(none)"
    alpha_note = (
        ""
        if evidence.alpha_by_ticker is not None
        else (
            "\n\nNote: live position-alpha data was unavailable for this pass — judge "
            "upheld/violated on PROCESS (did the decision follow the Tenet?), not P&L, "
            "and return est_cost_usd as null."
        )
    )
    return (
        "You are auditing ONE standing investment Tenet — a durable belief about HOW "
        "the owner invests — against the owner's OWN decisions made SINCE the Tenet "
        "was adopted. For EACH decision below, judge whether it UPHELD the Tenet, "
        "VIOLATED it, or is simply IRRELEVANT to it (cite an irrelevant decision in "
        "NEITHER list). Ground every verdict in a decision's exact [D<id>] token shown "
        "below — never invent one, never cite a decision not listed.\n\n"
        f"TENET ({tenet.scope_key}), adopted {tenet.as_of[:10]}:\n{tenet.body_md}\n\n"
        f"THE OWNER'S DECISIONS SINCE THEN:\n{decisions_block}"
        f"{alpha_note}\n\n"
        'Return JSON ONLY: {"upheld": ["<[D<id>] token>", ...], "violated": '
        '["<[D<id>] token>", ...], "est_cost_usd": <a number — your best-effort '
        "estimate of the dollar cost of the VIOLATED decisions using the position "
        "alpha figures shown above (negative = a loss from violating the Tenet), "
        "or null when no alpha data was shown or none of the violations have an "
        'alpha figure>, "one_liner": "<ONE sentence, at most 120 characters, in the '
        "owner's voice, summarizing the verdict (e.g. 'Held through two more "
        "high-conviction adds despite the exit-discipline Tenet — cost is showing "
        'up in NU.")"}.'
    )


def _default_call(prompt: str) -> dict[str, object] | None:
    """The live call — lazy-imported so unit tests never need the CLI. Raises
    on a parse/call failure (deliberately NOT swallowed to ``None`` here);
    ``assess_tenet``'s transient/hard-stop classification decides whether to
    defer-and-retry or propagate."""
    from llm.contracts import TENET_ACCOUNTABILITY_SCHEMA
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        scope="tenet_accountability",
        expect="object",
        schema=TENET_ACCOUNTABILITY_SCHEMA,
    )
    if not isinstance(obj, dict):
        return None
    return cast("dict[str, object]", obj)


def _coerce_ids(raw: object, valid_ids: set[int]) -> list[int]:
    """Grounding gate: every cited id must resolve to a ``decision_id``
    actually shown in the prompt. Accepts either a bracket token
    (``"[D12]"``/``"D12"``) or a bare int; anything else, or an id not in
    ``valid_ids``, is silently dropped — never guessed into existence."""
    out: list[int] = []
    if not isinstance(raw, list):
        return out
    for item in cast("list[object]", raw):
        token: str | None = None
        if isinstance(item, str):
            token = item.strip().strip("[]").upper()
            if token.startswith("D"):
                token = token[1:]
        elif isinstance(item, int) and not isinstance(item, bool):
            token = str(item)
        if not token:
            continue
        try:
            did = int(token)
        except ValueError:
            continue
        if did in valid_ids and did not in out:
            out.append(did)
    return sorted(out)


def _coerce_cost(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


def assess_tenet(
    tenet: InsightRow, evidence: Evidence, *, call: AssessCall | None = None
) -> Assessment | None:
    """One structured call auditing ``tenet`` against ``evidence.decisions``,
    deterministically validated before returning.

    Returns ``None`` (ZERO LLM calls) when there is no evidence to judge — a
    Tenet nobody has acted on since its ``as_of`` can be neither upheld nor
    violated. Also returns ``None`` on a TRANSIENT LLM failure (the caller is
    expected to tally ``deferred_transient`` in that case — see
    :func:`run_accountability`, which only invokes this after confirming
    evidence is non-empty, so it can tell the two ``None`` paths apart
    without this function threading an extra reason code). A hard stop
    (budget cap / missing CLI) propagates.
    """
    if not evidence.decisions:
        return None
    prompt = _build_prompt(tenet, evidence)
    do_call: AssessCall = call if call is not None else _default_call

    from llm.cli import is_hard_stop

    try:
        raw = do_call(prompt)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning(
            {
                "event": "tenet_accountability_transient",
                "tenet_id": tenet.id,
                "scope_key": tenet.scope_key,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
    if raw is None:
        return None

    valid_ids = {d.decision_id for d in evidence.decisions}
    upheld = _coerce_ids(raw.get("upheld"), valid_ids)
    # A decision the model cited in BOTH lists is a contradiction in its own
    # output — resolve deterministically by preferring "upheld" rather than
    # emitting an ambiguous double-counted id.
    violated = [d for d in _coerce_ids(raw.get("violated"), valid_ids) if d not in upheld]
    est_cost_usd = _coerce_cost(raw.get("est_cost_usd"))
    one_liner = str(raw.get("one_liner") or "").strip()[:_ONE_LINER_MAX_CHARS]
    if not one_liner:
        one_liner = f"upheld {len(upheld)} / violated {len(violated)} since {tenet.as_of[:10]}"
    return Assessment(
        upheld=upheld, violated=violated, est_cost_usd=est_cost_usd, one_liner=one_liner
    )


def persist_assessment(
    tenet: InsightRow,
    assessment: Assessment,
    *,
    db_path: Path | str,
    now: datetime | None = None,
) -> None:
    """Read-merge-write ``insight_notes.meta_json`` for ``tenet.id``: replaces
    the ``"accountability"`` key wholesale (this module keeps only the LATEST
    verdict — the weekly cron's own log is the history), preserving every
    OTHER meta key untouched (e.g. ``"tensions"``, written by the
    ``tenet_distill``/``session_distill`` paths). ``synthesis.insights`` has
    no public meta-merge helper, so this is a direct, single-connection SQL
    read-merge-write — the same bespoke-SQL idiom ``synthesis.tenets`` itself
    uses for ``approve_tenet``/``revert_tenet``."""
    stamp = (now or now_naive_utc()).isoformat()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT meta_json FROM insight_notes WHERE id = ?", (tenet.id,)
        ).fetchone()
        if row is None:
            return  # the tenet vanished between gather and persist — nothing to write
        meta: dict[str, object] = {}
        raw_meta = row[0]
        if raw_meta:
            try:
                parsed: object = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    meta = cast("dict[str, object]", parsed)
            except ValueError:
                meta = {}
        meta["accountability"] = {
            "as_of_run": stamp,
            "upheld": list(assessment.upheld),
            "violated": list(assessment.violated),
            "est_cost_usd": assessment.est_cost_usd,
            "one_liner": assessment.one_liner,
        }
        conn.execute(
            "UPDATE insight_notes SET meta_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), now_iso(), tenet.id),
        )
        conn.commit()
    finally:
        conn.close()


def run_accountability(
    db_path: Path | str, *, call: AssessCall | None = None, now: datetime | None = None
) -> dict[str, int]:
    """Weekly per-tenet sweep: gather -> assess -> persist, one Sonnet call
    per current Tenet that has evidence to judge.

    A per-tenet bug is logged and skipped (one bad Tenet never sinks the
    sweep); a hard stop propagates out of the sweep entirely — it is a
    systemic condition (blown budget / missing CLI) that would fail
    identically for every remaining Tenet, so continuing would just burn
    through the rest of the set for nothing."""
    from llm.cli import is_hard_stop

    tenets = list_tenets(status="current", db_path=db_path)
    counts = {
        "tenets": len(tenets),
        "assessed": 0,
        "skipped_no_evidence": 0,
        "deferred_transient": 0,
    }
    for tenet in tenets:
        try:
            evidence = gather_evidence(tenet, db_path=db_path)
            if not evidence.decisions:
                counts["skipped_no_evidence"] += 1
                continue
            assessment = assess_tenet(tenet, evidence, call=call)
            if assessment is None:
                # evidence.decisions was non-empty, so a None verdict here can
                # only be the transient-failure path (see assess_tenet's
                # docstring for why this pre-check is what lets the two
                # None-returning paths be told apart without an extra signal).
                counts["deferred_transient"] += 1
                continue
            persist_assessment(tenet, assessment, db_path=db_path, now=now)
            counts["assessed"] += 1
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.error(
                {
                    "event": "tenet_accountability_tenet_error",
                    "tenet_id": tenet.id,
                    "scope_key": tenet.scope_key,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
    return counts

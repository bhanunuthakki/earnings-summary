"""LLM-drafted exit post-mortems (B6, 2026-07-19 program overhaul).

``position_lifecycle.sync_position_lifecycle`` closes a ``position_entries``
row the instant a ticker leaves the portfolio, but the analyst's post-exit
grading — ``exit_reason`` / ``lessons`` / ``outcome_vs_thesis`` — stays NULL
until someone writes it from the holding page. In practice nobody does: prod
carries 13 closed rows with all three fields NULL. This module closes that
gap the same way B4/B5 closed the tenet/session gaps: one structured LLM call
per closed-but-ungraded position, drafting all three fields from the
ticker's own decision trail (``v_decision_journal``) plus, best-effort,
realized position alpha and the deterministic breach-history read
(``position_lifecycle.infer_outcome_vs_thesis`` — the SAME seam-9 prefill the
reconciler itself already runs at close time, re-derived here since more
thesis-evaluation history may have accrued since).

**Pending is DERIVED, not stamped** (no-new-columns rule, matching 0192/0193's
docstrings): a closed row counts as pending exactly when ALL THREE grading
fields are NULL (:func:`pending_postmortems`). ``apply_draft`` therefore only
ever writes a draft that fills all three at once (see :func:`draft_postmortem`
— an outcome label the model didn't return, or returned invalid, falls back
to the deterministic breach-history read; if THAT is unavailable too the
whole draft is discarded rather than landing two fields and leaving the third
NULL forever, which would silently drop the row out of ``pending`` while
never actually completing it).

**Provenance — the ``llm_draft`` marker.** ``position_entries`` has no JSON/
context column of its own (0088's schema is five typed exit-grading columns,
full stop) and this program's B-workstream rule is NO new tables/columns for
a synthesis pass. The existing slot for "how did this get written" is
``analyst_notes`` linked via its 0093 ``position_entry_id`` column — the same
link 0093 added for exactly this "durable annotation about a lifecycle row"
purpose — carrying ``source='advisor'`` (0077's existing provenance value for
"an LLM-authored artifact, not something the analyst typed themselves") and
``context_json['exit_postmortem_draft'] = true``. :func:`apply_draft` writes
this note in the SAME call that writes the grading fields; :func:`revert_draft`
and the nag governor's ``post_mortem`` moment class
(``src/research/governor.py``) both read it back to tell an LLM draft apart
from an owner's own grading — a marker that is never touched by a manual
edit through ``update_exit_fields`` directly, so a later owner edit made
outside this module's ``apply_draft``/``revert_draft`` pair leaves a stale
marker; a UI edit path is out of scope for this PR (see module docstring
note under :func:`revert_draft`) and should supersede/clear the marker when
built.

**AUTO-APPLIES with one-tap Revert** (owner ruling, mirrors B4's session-
distill auto-adopt): a draft lands live the instant it validates — no
propose-then-approve queue — because ``post_mortem`` COUNTS against the nag
governor's 1-2/day budget (it's the thing asking the owner to sanity-check),
so the governor itself is the review gate, not a separate ratification walk.

**Pacing.** The nightly 18:00 sweep leg drafts at most a FEW pending entries
per run (:data:`_DEFAULT_MAX_PER_RUN`) rather than the whole backlog — an
LLM-authored grading is exactly what the governor's daily cap exists to
pace, and drip-feeding keeps a big backlog from producing a wall of
post_mortem moments on one run. ``--postmortem-backfill``
(``execution/run_session_distill.py``) is the deliberate ONE-TIME exception
for the pre-existing 13 all-NULL prod rows: draft ALL pending in a single
batch, with ONE Telegram summary instead of N individual receipts (owner
ruling: one burst, receipts visible, no drip nagging).

Degrade-safety follows ``directives/llm_quota_scheduling.md`` rule 3: a
transient LLM failure (or an unusable/empty response) defers that entry —
tallied ``deferred_transient``, retried next sweep, nothing written — while a
hard stop (budget block / missing CLI) propagates so the runner exits
loudly. One bad entry never sinks the whole sweep.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from position_lifecycle import (
    OUTCOME_VOCAB,
    PositionEntry,
    get_entry,
    infer_outcome_vs_thesis,
    list_entries,
    update_exit_fields,
)
from user_state._db import open_conn
from user_state.notes import create_note

log = logging.getLogger(__name__)

PURPOSE = "exit_postmortem_draft"

# A draft call: the rendered prompt -> a raw dict, or None on "nothing
# usable". Injected so the engine is unit-testable without the CLI (mirrors
# synthesis.tenet_accountability.AssessCall).
DraftCall = Callable[[str], "dict[str, object] | None"]

_MAX_DECISIONS = 20
# Same posture as tenet_accountability._TRACKER_TIMEOUT_SECONDS: the tracker
# is a logon task with a real cold-start window, and this sweep must never
# block on it — a miss degrades alpha to None.
_TRACKER_TIMEOUT_SECONDS = 4.0
_TEXT_CAP = 500

# Pacing: the nightly sweep leg drafts at most this many pending entries per
# run (see module docstring "Pacing"). --postmortem-backfill overrides via
# batch=True (drafts every pending entry in one pass).
_DEFAULT_MAX_PER_RUN = 3

# The analyst_notes provenance marker apply_draft/_is_llm_draft/revert_draft
# share — see module docstring "Provenance".
_PROVENANCE_SOURCE = "advisor"
_PROVENANCE_NOTE_KIND = "observation"
_PROVENANCE_FLAG_KEY = "exit_postmortem_draft"


@dataclass(frozen=True, slots=True)
class DecisionTrailRow:
    """One owner/advisor decision on this ticker, shown to the model as
    ``[D<decision_id>]``."""

    decision_id: int
    kind: str  # recommendation_kind: 'add' | 'trim' | 'hold' | 'sell' | ...
    conviction: str | None
    outcome_label: str | None
    outcome_pct: float | None
    made_at: str
    falsifier: str | None


@dataclass(frozen=True, slots=True)
class Evidence:
    """The deterministic, zero-LLM evidence bundle for one entry's draft."""

    decisions: list[DecisionTrailRow]
    alpha_since_entry: float | None
    inferred_outcome: str | None  # position_lifecycle.infer_outcome_vs_thesis's read


@dataclass(frozen=True, slots=True)
class Draft:
    """A validated draft — ``outcome_vs_thesis`` is always a real
    :data:`position_lifecycle.OUTCOME_VOCAB` member, never a fabricated
    label (see :func:`draft_postmortem`)."""

    exit_reason: str
    lessons: str
    outcome_vs_thesis: str


def pending_postmortems(db_path: Path | str) -> list[PositionEntry]:
    """Closed ``position_entries`` whose ``exit_reason`` / ``lessons`` /
    ``outcome_vs_thesis`` are ALL NULL — pending is DERIVED (no stamp
    column; see module docstring). Newest-closed-first, ``list_entries``'s
    own ordering."""
    entries = list_entries(db_path=db_path, limit=1000)
    return [
        e
        for e in entries
        if not e.is_open
        and e.exit_reason is None
        and e.lessons is None
        and e.outcome_vs_thesis is None
    ]


def drafted_awaiting_glance(db_path: Path | str) -> list[PositionEntry]:
    """Closed ``position_entries`` whose three grading fields are ALL
    filled AND were filled by :func:`apply_draft` (the ``llm_draft``
    provenance note) — the source list for the governor's ``post_mortem``
    moment class. Distinct from :func:`pending_postmortems`: this is the
    "drafted, awaiting an owner glance" state, not the "not yet drafted"
    state."""
    entries = list_entries(db_path=db_path, limit=1000)
    return [
        e
        for e in entries
        if not e.is_open
        and e.exit_reason is not None
        and e.lessons is not None
        and e.outcome_vs_thesis is not None
        and _is_llm_draft(e.id, db_path=db_path)
    ]


def is_awaiting_glance(entry_id: int, *, db_path: Path | str) -> bool:
    """Freshness re-check for ONE referenced entry (the governor's
    ``post_mortem`` moment re-verifies the SPECIFIC entry it fired on, not
    the whole ``drafted_awaiting_glance`` collection — same pattern every
    other class in ``research.governor`` uses). Can't verify → False."""
    entry = get_entry(entry_id, db_path=db_path)
    if entry is None or entry.is_open:
        return False
    if entry.exit_reason is None or entry.lessons is None or entry.outcome_vs_thesis is None:
        return False
    return _is_llm_draft(entry_id, db_path=db_path)


def _is_llm_draft(entry_id: int, *, db_path: Path | str) -> bool:
    """Does a linked ``analyst_notes`` row carry the ``llm_draft``
    provenance marker for this entry? Decodes ``context_json`` in Python
    (not SQL ``json_extract``) for portability across SQLite builds without
    the json1 extension — the same posture ``user_state.notes.list_triage_notes``
    documents. Can't verify (missing DB / pre-0093 schema) → False."""
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, OSError):
        return False
    try:
        rows = conn.execute(
            "SELECT context_json FROM analyst_notes WHERE position_entry_id = ? AND source = ?",
            (entry_id, _PROVENANCE_SOURCE),
        ).fetchall()
    except sqlite3.OperationalError:
        return False  # pre-0093 schema — no position_entry_id column
    finally:
        conn.close()
    for row in rows:
        raw = row[0]
        if not raw:
            continue
        try:
            ctx: object = json.loads(raw)
        except ValueError:
            continue
        if (
            isinstance(ctx, dict)
            and cast("dict[str, object]", ctx).get(_PROVENANCE_FLAG_KEY) is True
        ):
            return True
    return False


def _fetch_alpha(entry: PositionEntry, *, db_path: Path | str) -> float | None:
    """This ticker's realized alpha since ``entry.entry_date``, via the
    shared tracker client. ``None`` on ANY problem (offline/cold-starting
    tracker, no alpha row for this ticker) — belt-and-braces, exactly
    ``tenet_accountability._fetch_alpha``'s posture."""
    try:
        from integrations.portfolio_tracker_client import fetch_portfolio_analytics

        analytics = fetch_portfolio_analytics(
            only={"position_alpha"},
            start_date=entry.entry_date[:10] if entry.entry_date else None,
            timeout=_TRACKER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.debug(
            {
                "event": "exit_postmortem_alpha_unavailable",
                "entry_id": entry.id,
                "ticker": entry.ticker,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
    if analytics.position_alpha is None:
        return None
    for row in analytics.position_alpha.rows:
        if row.ticker and row.ticker.upper() == entry.ticker.upper() and row.alpha is not None:
            return row.alpha
    return None


def _infer_outcome(entry: PositionEntry, *, db_path: Path | str) -> str | None:
    """Re-derive the seam-9 breach-history read at DRAFT time (not just the
    reconciler's close-time snapshot) — more ``thesis_evaluations`` history
    may have accrued since the row closed. Best-effort: any problem → None."""
    if entry.exit_date is None:
        return None
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        return infer_outcome_vs_thesis(conn, entry.ticker, entry.exit_date)
    except Exception:
        return None
    finally:
        conn.close()


def gather_evidence(entry: PositionEntry, *, db_path: Path | str) -> Evidence:
    """Deterministic, zero-LLM: this ticker's decision trail
    (``v_decision_journal``, chronological, capped at :data:`_MAX_DECISIONS`),
    realized alpha since entry, and the deterministic outcome hint."""
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, OSError):
        rows: list[sqlite3.Row] = []
    else:
        try:
            params: list[object] = [entry.ticker]
            date_clause = ""
            if entry.entry_date:
                date_clause = " AND made_at >= ?"
                params.append(entry.entry_date)
            params.append(_MAX_DECISIONS)
            rows = conn.execute(
                f"""
                SELECT decision_id, recommendation_kind, conviction,
                       outcome_label, outcome_pct, made_at, falsifier
                FROM v_decision_journal
                WHERE ticker = ?{date_clause}
                ORDER BY made_at ASC, decision_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []  # pre-0179 schema — no v_decision_journal to read
        finally:
            conn.close()
    decisions = [
        DecisionTrailRow(
            decision_id=int(r[0]),
            kind=str(r[1]),
            conviction=str(r[2]) if r[2] is not None else None,
            outcome_label=str(r[3]) if r[3] is not None else None,
            outcome_pct=float(r[4]) if r[4] is not None else None,
            made_at=str(r[5]),
            falsifier=str(r[6]) if r[6] is not None else None,
        )
        for r in rows
    ]
    return Evidence(
        decisions=decisions,
        alpha_since_entry=_fetch_alpha(entry, db_path=db_path),
        inferred_outcome=_infer_outcome(entry, db_path=db_path),
    )


def _build_prompt(entry: PositionEntry, evidence: Evidence) -> str:
    lines: list[str] = []
    for d in evidence.decisions:
        conv_txt = f", conviction={d.conviction}" if d.conviction else ""
        outcome_txt = ""
        if d.outcome_label:
            outcome_txt = f", outcome={d.outcome_label}"
            if d.outcome_pct is not None:
                outcome_txt += f" ({d.outcome_pct:+.1f}%)"
        falsifier_txt = f' -- falsifier: "{d.falsifier}"' if d.falsifier else ""
        lines.append(
            f"[D{d.decision_id}] {d.made_at[:10]} {d.kind.upper()}"
            f"{conv_txt}{outcome_txt}{falsifier_txt}"
        )
    trail_block = "\n".join(lines) or "(no decisions on record for this ticker)"

    alpha_txt = (
        f"${evidence.alpha_since_entry:,.0f}"
        if evidence.alpha_since_entry is not None
        else "unavailable"
    )
    hint_txt = evidence.inferred_outcome or "none available"
    entry_txt = entry.entry_date or "unknown"
    exit_txt = entry.exit_date or "unknown"
    entry_price_txt = f" at ${entry.entry_price:,.2f}" if entry.entry_price is not None else ""
    exit_price_txt = f" at ${entry.exit_price:,.2f}" if entry.exit_price is not None else ""
    move_txt = "unavailable"
    if entry.entry_price is not None and entry.exit_price is not None and entry.entry_price:
        pct = (entry.exit_price - entry.entry_price) / entry.entry_price * 100
        move_txt = f"{pct:+.1f}%"
    thesis_txt = entry.entry_thesis_excerpt or "(no thesis excerpt on file)"

    return (
        "You are drafting an EXIT POST-MORTEM for one closed position -- a short, "
        "honest grading of why it exited and what it taught, in the owner's own "
        "voice. Ground every claim in the decision trail and figures shown below; "
        "never invent a decision or number not shown.\n\n"
        f"TICKER: {entry.ticker}\n"
        f"ENTRY: {entry_txt}{entry_price_txt}\n"
        f"EXIT: {exit_txt}{exit_price_txt}\n"
        f"PRICE MOVE: {move_txt}\n"
        f"REALIZED ALPHA SINCE ENTRY: {alpha_txt}\n"
        f"ENTRY THESIS: {thesis_txt}\n"
        f"DETERMINISTIC BREACH-HISTORY READ (a hint, not a verdict): {hint_txt}\n\n"
        f"DECISION TRAIL:\n{trail_block}\n\n"
        'Return JSON ONLY: {"exit_reason": "<one or two sentences -- WHY the '
        'position was exited, grounded in the trail/price move above>", '
        '"lessons": "<one or two sentences -- what this position taught, in the '
        "owner's voice, useful for the NEXT similar decision>\", "
        '"outcome_vs_thesis": "played_out"|"broke"|"mixed"|"unrelated" '
        "(played_out = the thesis held and this was a natural conclusion; broke = "
        "the thesis itself broke; mixed = partially validated, partially not; "
        "unrelated = exited for reasons unconnected to the original thesis, e.g. "
        "portfolio-level rebalancing)}."
    )


def _default_call(prompt: str) -> dict[str, object] | None:
    """The live call -- lazy-imported so unit tests never need the CLI."""
    from llm.structured import call_llm_structured

    obj = call_llm_structured(prompt, purpose=PURPOSE, scope="exit_postmortem", expect="object")
    if not isinstance(obj, dict):
        return None
    return cast("dict[str, object]", obj)


def draft_postmortem(
    entry: PositionEntry,
    *,
    db_path: Path | str,
    call: DraftCall | None = None,
    evidence: Evidence | None = None,
) -> Draft | None:
    """One structured call drafting ``entry``'s exit post-mortem,
    deterministically validated before returning.

    ``evidence`` lets a caller that already gathered it (``run_postmortem_drafts``,
    to decide whether there is anything to draft before spending an LLM call)
    pass it straight through instead of re-querying — otherwise it is
    gathered here.

    Returns ``None`` (ZERO LLM calls) when there is no decision trail to
    draft from -- a position nobody made a recorded decision on can't be
    honestly narrated. Also returns ``None`` on a TRANSIENT LLM failure, or
    when the response is unusable (empty text, or an invalid outcome label
    with no deterministic fallback available) -- all three are retried next
    sweep by the caller (:func:`run_postmortem_drafts`), which tallies them
    together as ``deferred_transient`` since none of them is a permanent
    "give up" state. A hard stop (budget cap / missing CLI) propagates.
    """
    if evidence is None:
        evidence = gather_evidence(entry, db_path=db_path)
    if not evidence.decisions:
        return None

    prompt = _build_prompt(entry, evidence)
    do_call: DraftCall = call if call is not None else _default_call

    from llm.cli import is_hard_stop

    try:
        raw = do_call(prompt)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning(
            {
                "event": "exit_postmortem_transient",
                "entry_id": entry.id,
                "ticker": entry.ticker,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
    if raw is None:
        return None

    exit_reason = str(raw.get("exit_reason") or "").strip()[:_TEXT_CAP]
    lessons = str(raw.get("lessons") or "").strip()[:_TEXT_CAP]
    if not exit_reason or not lessons:
        log.warning(
            {"event": "exit_postmortem_unusable", "entry_id": entry.id, "reason": "empty_text"}
        )
        return None

    outcome_raw = str(raw.get("outcome_vs_thesis") or "").strip().lower()
    outcome = outcome_raw if outcome_raw in OUTCOME_VOCAB else evidence.inferred_outcome
    if outcome not in OUTCOME_VOCAB:
        # Never fabricate an enum member: no valid label from the model AND
        # no deterministic fallback -> the draft is unusable, not half-landed.
        log.warning(
            {
                "event": "exit_postmortem_unusable",
                "entry_id": entry.id,
                "reason": "no_valid_outcome",
            }
        )
        return None

    return Draft(exit_reason=exit_reason, lessons=lessons, outcome_vs_thesis=outcome)


def apply_draft(entry: PositionEntry, draft: Draft, *, db_path: Path | str) -> bool:
    """Write ``draft`` onto ``entry`` via ``update_exit_fields``, WRITE-ONCE:
    any of the three fields already non-NULL on ``entry`` is left untouched
    (never overwrites an owner's or a prior grading -- a defensive re-check
    against a race between :func:`pending_postmortems` selecting this entry
    and this call landing). Also lands the ``llm_draft`` provenance note
    (see module docstring). Returns whether anything was written."""
    fields: dict[str, str] = {}
    if entry.exit_reason is None:
        fields["exit_reason"] = draft.exit_reason
    if entry.lessons is None:
        fields["lessons"] = draft.lessons
    if entry.outcome_vs_thesis is None:
        fields["outcome_vs_thesis"] = draft.outcome_vs_thesis
    if not fields:
        return False  # already graded -- nothing left to write

    ok = update_exit_fields(db_path=db_path, entry_id=entry.id, **fields)
    if ok:
        try:
            create_note(
                user_id=entry.user_id,
                ticker=entry.ticker,
                kind=_PROVENANCE_NOTE_KIND,
                body=(
                    f"Drafted exit post-mortem for {entry.ticker} "
                    f"({draft.outcome_vs_thesis}): {draft.exit_reason}"
                ),
                source=_PROVENANCE_SOURCE,
                position_entry_id=entry.id,
                context={_PROVENANCE_FLAG_KEY: True},
                db_path=db_path,
            )
        except Exception:  # best-effort -- the grading already landed
            log.warning({"event": "exit_postmortem_provenance_note_failed", "entry_id": entry.id})
    return ok


def revert_draft(entry_id: int, *, db_path: Path | str) -> bool:
    """Owner-tap Revert: NULL exactly ``exit_reason``/``lessons``/
    ``outcome_vs_thesis`` IFF the linked provenance note shows these were an
    LLM draft (:func:`_is_llm_draft`) -- an owner's own manually-typed
    grading is never touched, since it carries no such note.

    UI hook (not built in this PR): a Revert button on the holding-page
    lifecycle panel / journal, wired the same way ``synthesis.adoption_notify``'s
    Telegram Revert routes through ``capture.research_notify.dispatch_callback``,
    would call this with the ping's referenced ``entry_id``.
    """
    if not _is_llm_draft(entry_id, db_path=db_path):
        return False
    return update_exit_fields(
        db_path=db_path, entry_id=entry_id, exit_reason="", lessons="", outcome_vs_thesis=""
    )


def _compose_batch_summary(drafted: list[tuple[str, str]]) -> str:
    items = "; ".join(f"{ticker} ({outcome})" for ticker, outcome in drafted)
    n = len(drafted)
    noun = "post-mortem" if n == 1 else "post-mortems"
    return f"Drafted {n} exit {noun}: {items} -- review in the journal; each is revertible."


def _send_batch_summary(text: str, *, db_path: Path | str, repo_root: Path | str | None) -> None:
    """Best-effort single Telegram push for the batch-drafted post-mortems
    (owner ruling: one burst, receipts visible, no drip nagging -- see
    module docstring "Pacing"). Mirrors
    ``synthesis.adoption_notify.announce_adoption``'s config-skip contract
    exactly: a missing bot token/chat id degrades to a silent skip, never a
    raised exception into the caller's sweep."""
    try:
        from capture import telegram
        from capture.token_store import CaptureSetupError, load_chat_id, load_token

        root = Path(repo_root) if repo_root is not None else Path(db_path).resolve().parent.parent
        try:
            token = load_token()
        except CaptureSetupError:
            token = None
        chat_id = load_chat_id(root / "data" / "capture" / "telegram_chat_id.json")
        if not token or not chat_id:
            log.info(
                {"event": "exit_postmortem_batch_summary_skipped", "reason": "no_telegram_config"}
            )
            return
        telegram.send_message(token, chat_id, text)
        log.info({"event": "exit_postmortem_batch_summary_sent"})
    except Exception as exc:  # best-effort -- never break the caller's sweep
        log.warning({"event": "exit_postmortem_batch_summary_failed", "error": str(exc)})


def run_postmortem_drafts(
    db_path: Path | str,
    *,
    call: DraftCall | None = None,
    batch: bool = False,
    repo_root: Path | str | None = None,
    max_per_run: int = _DEFAULT_MAX_PER_RUN,
) -> dict[str, int]:
    """Sweep pending post-mortems: gather -> draft -> apply, one Sonnet call
    per entry that has a decision trail to draft from.

    ``batch=False`` (the nightly leg) drafts at most ``max_per_run`` pending
    entries, oldest-selection-order first, and never sends a summary.
    ``batch=True`` (``--postmortem-backfill``) drafts EVERY pending entry and,
    if at least one drafted, sends ONE Telegram summary.

    A per-entry bug is logged and skipped (one bad entry never sinks the
    sweep); a hard stop propagates out of the sweep entirely -- systemic
    (blown budget / missing CLI), so continuing would just burn through the
    rest of the pending set for nothing."""
    from llm.cli import is_hard_stop

    pending = pending_postmortems(db_path)
    counts = {"pending": len(pending), "drafted": 0, "deferred_transient": 0, "skipped": 0}
    targets = pending if batch else pending[:max_per_run]
    landed: list[tuple[str, str]] = []

    for entry in targets:
        try:
            evidence = gather_evidence(entry, db_path=db_path)
            if not evidence.decisions:
                counts["skipped"] += 1
                continue
            draft = draft_postmortem(entry, db_path=db_path, call=call, evidence=evidence)
            if draft is None:
                counts["deferred_transient"] += 1
                continue
            if apply_draft(entry, draft, db_path=db_path):
                counts["drafted"] += 1
                landed.append((entry.ticker, draft.outcome_vs_thesis))
            else:
                counts["skipped"] += 1  # raced with a manual grade in between
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.error(
                {
                    "event": "exit_postmortem_entry_error",
                    "entry_id": entry.id,
                    "ticker": entry.ticker,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

    if batch and landed:
        _send_batch_summary(_compose_batch_summary(landed), db_path=db_path, repo_root=repo_root)

    return counts

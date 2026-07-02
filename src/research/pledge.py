"""The pre-buy pledge channel — entry-time coaching (2026-07-02 grill, decision #5).

The owner's #1 self-named entry weakness: "the catalyst test must pass BEFORE
I buy, not after." The agreed mechanic is a 10-second pledge — "buying NVO
~$30k" said to the bot before ordering — answered by a grounded catalyst-test
challenge; the daily retro net (``research.decision_feed``) catches anything
unannounced.

Flow (both Telegram and the tray, voice included — the tap reads the landed
note body):

1. the musing lands normally (words durably stored FIRST — capture invariant);
2. a deterministic regex pre-gate spots pledge-shaped text (zero tokens on a
   miss);
3. the governed ``musing_decision_extract`` (#718) fills
   {ticker, direction, size_pct, conviction, falsifier};
4. ``capture_decision`` persists the owner decision via the 0130 adapters,
   linked to the musing; dollar sizes parse from the "~$30k" idiom;
5. the challenge reply carries the catalyst test + the LLM-free
   position-review pre-analysis read (weight, valuation gap, tripped rules) so
   the challenge argues numbers, not vibes — and asks for whatever the pledge
   didn't state (conviction / falsifier).

Annotation: a follow-up message ("high conviction, falsifier: NPL >5% 2Q")
fills ONLY the NULL fields of the newest annotation-pending owner decision —
conviction/falsifier stay WRITE-ONCE (a set value is never overwritten).

Everything is fire-and-forget from the capture path: a pledge failure must
never affect the landing. Outcomes leave 'captured' audit rows
(detail='pledge:decision:<id>') so a dead pledge tap is distinguishable from
no pledges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from user_state._db import now_iso, open_conn

# Pledge-shaped text: a stated (imminent) position change. Deliberately
# narrow — "should I buy?" is a wondering, not a pledge; "bought last year"
# is a memory. The retro net covers what this gate misses.
_PLEDGE_RE = re.compile(
    r"\b(?:"
    r"buying|selling|trimming|adding\s+to"
    r"|about\s+to\s+(?:buy|sell|add|trim)"
    r"|i(?:'m|\s+am)\s+(?:buying|selling|adding|trimming)"
    r"|pledge[:\s]"
    r")\b",
    re.IGNORECASE,
)

# Annotation-shaped follow-up: names a conviction level or a falsifier.
_ANNOTATION_RE = re.compile(r"\b(?:conviction|falsifier|high|medium|low)\b", re.IGNORECASE)

_SIZE_RE = re.compile(r"[~≈]?\$(\d+(?:\.\d+)?)\s*[kK]\b")
_PLEDGE_DIRECTIONS = frozenset({"buy", "sell", "add", "trim"})

PLEDGE_MARKER = "pledge:note:"
ANNOTATION_WINDOW_HOURS = 48

_CATALYST_TEST = (
    "The catalyst test (your rule — it must pass BEFORE you order):\n"
    "1. What is the near-term catalyst, in one sentence?\n"
    "2. Is it already priced in — what does the market believe?\n"
    "3. Washout or value trap: what distinguishes this one?"
)


@dataclass(frozen=True, slots=True)
class PledgeCapture:
    decision_id: int
    ticker: str
    direction: str
    missing: tuple[str, ...]  # of ('conviction', 'falsifier')


def _audit(
    *, note_id: int, channel: str, detail: str, llm_ran: bool, db_path: Path | str | None
) -> None:
    try:
        from capture.audit import write_audit

        write_audit(
            channel=channel,
            action="captured",
            utterance_summary=f"pledge tap: {detail}",
            note_id=note_id,
            detail=detail,
            purpose="musing_decision_extract" if llm_ran else None,
            db_path=db_path,
        )
    except Exception:  # observability must never affect capture
        pass


def detect_and_capture_pledge(
    note_id: int,
    *,
    channel: str = "capture",
    db_path: Path | str | None = None,
    extract_call: object | None = None,
) -> PledgeCapture | None:
    """The fire-and-forget pledge tap. Returns the capture, or None.

    NEVER raises — a pledge failure must not affect the landing."""
    try:
        from user_state.notes import get_note

        note = get_note(note_id, db_path=db_path)
        if note is None or note.kind != "musing":
            return None
        body = note.body or ""
        if not _PLEDGE_RE.search(body):
            return None

        conn = open_conn(db_path)
        try:
            if conn.execute(
                "SELECT 1 FROM decisions WHERE user_notes LIKE ?",
                (f"%{PLEDGE_MARKER}{note_id}%",),
            ).fetchone():
                return None  # already captured for this note
        finally:
            conn.close()

        from research.decision_capture import capture_decision, extract_decision
        from research.decision_feed import link_decision_to_note, persist_owner_decision

        extracted = extract_decision(body, call=extract_call)  # type: ignore[arg-type]
        direction = str(extracted.get("direction") or "").lower()
        ticker = str(extracted.get("ticker") or "").upper() or None
        if direction not in _PLEDGE_DIRECTIONS or not ticker:
            _audit(
                note_id=note_id,
                channel=channel,
                detail="pledge:unparsed",
                llm_ran=True,
                db_path=db_path,
            )
            return None

        size_m = _SIZE_RE.search(body)
        size_usd = float(size_m.group(1)) * 1000.0 if size_m else None
        conviction = extracted.get("conviction")
        falsifier = extracted.get("falsifier")

        def _persist(**kw: object) -> int:
            return persist_owner_decision(
                ticker=str(kw.get("ticker") or ticker),
                direction=str(kw.get("direction") or direction),
                size_pct=(
                    float(v) if isinstance((v := kw.get("size_pct")), (int, float)) else None
                ),
                conviction=str(kw["conviction"]) if kw.get("conviction") else None,
                falsifier=str(kw["falsifier"]) if kw.get("falsifier") else None,
                size_usd=size_usd,
                rationale=body,
                user_notes=f"{PLEDGE_MARKER}{note_id} · pre-buy pledge ({channel})",
                note_id=int(n) if isinstance((n := kw.get("note_id")), int) else None,
                db_path=db_path,
            )

        decision_id = capture_decision(
            text=body,
            ticker=ticker,
            direction=direction,
            size_pct=(
                float(sp) if isinstance((sp := extracted.get("size_pct")), (int, float)) else None
            ),
            conviction=str(conviction) if conviction else None,
            falsifier=str(falsifier) if falsifier else None,
            note_id=note_id,
            db_path=db_path,
            persist_fn=_persist,
            link_fn=link_decision_to_note,
        )
        if decision_id is None:
            _audit(
                note_id=note_id,
                channel=channel,
                detail="pledge:below_threshold",
                llm_ran=True,
                db_path=db_path,
            )
            return None
        missing = tuple(
            field
            for field, value in (("conviction", conviction), ("falsifier", falsifier))
            if not value
        )
        _audit(
            note_id=note_id,
            channel=channel,
            detail=f"pledge:decision:{decision_id}",
            llm_ran=True,
            db_path=db_path,
        )
        return PledgeCapture(
            decision_id=int(decision_id), ticker=ticker, direction=direction, missing=missing
        )
    except Exception:
        _audit(
            note_id=note_id,
            channel=channel,
            detail="pledge:errored",
            llm_ran=True,
            db_path=db_path,
        )
        return None


def build_challenge(
    pledge: PledgeCapture,
    *,
    repo_root: Path | None = None,
    db_path: Path | str | None = None,
) -> str:
    """The catalyst-test challenge reply — grounded by the LLM-free
    position-review pre-analysis when available, degrading to the test alone."""
    head = (
        f"Pledge recorded (#{pledge.decision_id}): {pledge.direction.upper()} "
        f"{pledge.ticker}.\n\n{_CATALYST_TEST}"
    )
    read = ""
    if repo_root is not None:
        try:
            from advisor.position_review import build_pre_analysis, render_pre_analysis_chat

            pre = build_pre_analysis(repo_root, pledge.ticker, db_path=db_path)
            read = "\n\nCurrent read:\n" + render_pre_analysis_chat(pre)
        except Exception:
            read = ""  # the challenge stands on its own
    ask = ""
    if pledge.missing:
        wants = " and ".join(pledge.missing)
        ask = (
            f"\n\nReply with your {wants} to complete the record — e.g. "
            '"high conviction, falsifier: 15-90d NPL >5% for 2Q".'
        )
    return head + read + ask


def annotate_latest_pending(
    text: str,
    *,
    db_path: Path | str | None = None,
    extract_call: object | None = None,
) -> int | None:
    """Fill the NULL conviction/falsifier of the newest annotation-pending
    owner decision from a follow-up message. WRITE-ONCE: set values are never
    overwritten; only NULLs fill. Returns the decision id, or None.

    Pre-gates: annotation-shaped text (regex) + a pending stub within the
    window — both checked before any token is spent."""
    if not _ANNOTATION_RE.search(text or ""):
        return None
    from datetime import timedelta

    from user_state._db import now_naive_utc

    # Cutoff computed in Python — created_at is naive-UTC ISO with a 'T';
    # sqlite's datetime('now') space-format would compare unevenly.
    cutoff = (now_naive_utc() - timedelta(hours=ANNOTATION_WINDOW_HOURS)).isoformat()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            """
            SELECT id, conviction, falsifier FROM decisions
            WHERE decided_by = 'owner'
              AND (conviction IS NULL OR falsifier IS NULL)
              AND created_at >= ?
              AND (user_notes LIKE ? OR user_notes LIKE '%retro-net:%')
            ORDER BY id DESC LIMIT 1
            """,
            (cutoff, f"%{PLEDGE_MARKER}%"),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    decision_id, have_conviction, have_falsifier = int(row[0]), row[1], row[2]

    from research.decision_capture import extract_decision

    try:
        extracted = extract_decision(text, call=extract_call)  # type: ignore[arg-type]
    except Exception:
        return None
    sets: dict[str, str] = {}
    if have_conviction is None and extracted.get("conviction"):
        sets["conviction"] = str(extracted["conviction"])
    if have_falsifier is None and extracted.get("falsifier"):
        sets["falsifier"] = str(extracted["falsifier"])
    if not sets:
        return None
    conn = open_conn(db_path)
    try:
        assignments = ", ".join(f"{col} = ?" for col in sets)
        conn.execute(
            f"UPDATE decisions SET {assignments} WHERE id = ?",
            (*sets.values(), decision_id),
        )
        conn.execute(
            "UPDATE decisions SET user_notes = user_notes || ? WHERE id = ?",
            (f" · annotated {now_iso()}", decision_id),
        )
        conn.commit()
    finally:
        conn.close()
    return decision_id

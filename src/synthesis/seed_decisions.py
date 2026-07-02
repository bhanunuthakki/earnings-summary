"""Seed-corpus → decisions-table backfill (the Brier denominator, day one).

The 2026-07-02 grill un-deferred true Brier calibration and chose to seed the
denominator by retro-grading the owner's 22 brokerage-reconciled seed decisions
(``data/ledger_seed/seed.json``) instead of waiting ~90 days for fresh ones.
This module lands them as ``decided_by='owner'`` rows in the ONE calibration
ledger (the ``decisions`` table, 0130 shape), deterministically:

- action → recommendation_kind (buy→initiate; add/trim/sell verbatim)
- ``approx_date`` 'YYYY-MM' → mid-month ``made_at`` (the seed's own provenance
  note says recall was reconciled against the brokerage ledger, but dates stay
  approximate — the grader's ±5% band is robust to a two-week offset)
- dollar sizes parsed from the rationale's "~$26k" idiom; ETF tickers get
  ``instrument='etf'``; an explicit "Roth" mention sets ``account='roth'``
- the falsifier lands VERBATIM, including any "(inferred)" marker — the coach
  may only quote falsifiers the owner has ratified (marker removed by the
  reconciliation pass); until then they are data, not ammunition
- rows are hindsight-labeled via ``user_notes`` ('seed:decision:<n> ·
  retro-graded backfill (hindsight-labeled)') which doubles as the
  idempotency key — re-running skips existing items

Outcome grades are NOT hand-coded: rows land ``outcome_label='pending'``-shaped
(outcome columns NULL) and the standing ``execution/grade_decisions.py`` price
grader grades them with the same methodology as every advisor row.

Also lands the one standing intent the corpus names 3+ times — the far-OTM
LEAP sleeve — as a ``kind='intent'`` note **pre-marked resolved-rejected**
(the owner killed the NVDA-LEAP path in a Claude chat ~2026-07-01; the corpus
still carried it as live. Coaching against it would be the trust-destroying
stale nag the freshness rule exists to prevent).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import cast

from user_state._db import now_iso, open_conn
from user_state.notes import create_note, resolve_note

_ACTION_TO_KIND = {"buy": "initiate", "add": "add", "trim": "trim", "sell": "sell"}
_ETF_TICKERS = frozenset({"FLKR", "XLV", "SGOV"})
_SIZE_RE = re.compile(r"[~≈]?\$(\d+(?:\.\d+)?)\s*[kK]\b")
_ROTH_RE = re.compile(r"\broth\b", re.IGNORECASE)

_LEAP_INTENT_REF = "seed:intent:leap-sleeve"
_LEAP_INTENT_BODY = (
    "Standing intent (named 3+ times across the 12-mo corpus): keep the core of a "
    "high-conviction winner and express further upside via a far-OTM long-dated LEAP "
    "sleeve (5-20%) instead of selling early. RESOLVED-REJECTED 2026-07-01 in a Claude "
    "Code chat: selling NVO to fund NVDA LEAPs deletes the portfolio's one hedge and "
    "adds leveraged exposure to the single largest position — decided NOT to pursue."
)


def _made_at(approx: str) -> str:
    """'YYYY-MM' → mid-month naive-UTC stamp; a full date passes through."""
    raw = (approx or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return f"{raw}-15T00:00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return f"{raw}T00:00:00"
    return now_iso()


def _size_usd(rationale: str) -> float | None:
    m = _SIZE_RE.search(rationale or "")
    return float(m.group(1)) * 1000.0 if m else None


def backfill_seed_decisions(db_path: Path | str | None, seed_path: Path | str) -> dict[str, int]:
    """Land the seed corpus's decisions as owner rows in ``decisions``.

    Idempotent per item; returns a tally. Requires the 0130 schema (raises
    sqlite3.OperationalError on a pre-0130 DB — run ``alembic upgrade head``)."""
    payload = cast("dict[str, object]", json.loads(Path(seed_path).read_text(encoding="utf-8")))
    decisions = cast("list[dict[str, object]]", payload.get("decisions") or [])
    tally = {"inserted": 0, "skipped_existing": 0, "skipped_unmapped": 0, "intent": 0}
    conn = open_conn(db_path)
    try:
        for idx, dec in enumerate(decisions, start=1):
            marker = f"seed:decision:{idx}"
            exists = conn.execute(
                "SELECT 1 FROM decisions WHERE decided_by='owner' AND user_notes LIKE ?",
                (marker + " %",),
            ).fetchone()
            if exists:
                tally["skipped_existing"] += 1
                continue
            action = str(dec.get("action") or "").lower()
            kind = _ACTION_TO_KIND.get(action)
            ticker = str(dec.get("ticker") or "").upper() or None
            if kind is None or ticker is None:
                tally["skipped_unmapped"] += 1
                continue
            rationale = str(dec.get("rationale") or "")
            falsifier = str(dec.get("falsifier") or "") or None
            conviction = str(dec.get("conviction") or "") or None
            stamp = now_iso()
            conn.execute(
                """
                INSERT INTO decisions
                    (ticker, recommendation_kind, conviction, decided_by, scope,
                     instrument, account, size_usd, falsifier, rationale_excerpt,
                     source_prose, user_notes, made_at, created_at)
                VALUES (?, ?, ?, 'owner', 'ticker', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    kind,
                    conviction,
                    "etf" if ticker in _ETF_TICKERS else "equity",
                    "roth" if _ROTH_RE.search(rationale) else None,
                    _size_usd(rationale),
                    falsifier,
                    rationale[:512],
                    rationale,
                    f"{marker} · retro-graded backfill (hindsight-labeled)",
                    _made_at(str(dec.get("approx_date") or "")),
                    stamp,
                ),
            )
            tally["inserted"] += 1
        conn.commit()
    finally:
        conn.close()

    if _land_leap_intent(db_path):
        tally["intent"] = 1
    return tally


def _land_leap_intent(db_path: Path | str | None) -> bool:
    """The LEAP-sleeve standing intent, pre-marked resolved-rejected.

    Idempotent the same way seed._seed_note is: the partial-unique index on
    ``source_ref`` refuses a duplicate."""
    try:
        row = create_note(
            ticker=None,
            kind="intent",
            body=_LEAP_INTENT_BODY,
            source="capture",
            source_ref=_LEAP_INTENT_REF,
            context={
                "status": "resolved-rejected",
                "closed_by": "claude_session:ledger-seed-lineage",
                "closed_at": "2026-07-01",
                "reason": "deletes the NVO hedge; concentrates the largest exposure",
            },
            db_path=db_path,
        )
    except sqlite3.IntegrityError:
        return False
    resolve_note(row.id, resolution_note="resolved-rejected before landing", db_path=db_path)
    return True

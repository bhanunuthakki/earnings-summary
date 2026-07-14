"""Restatement sensor — push when the source corrects a held-name number (L10 PR2).

A supersede (a later filing materially restating an already-reported figure) is
the most decision-relevant provenance event the platform produced but never
pushed: the restatement chain was written (``supersedes_id``) and surfaced on a
passive panel, but no trigger fired, so the owner only learned a held-name number
had changed by going to look. This sensor closes that loop.

``Cadence.DAILY``. ``scan`` walks the financial_facts + kpi_facts supersede
chains for a HELD ticker (``tracked_companies.list_type = 'portfolio'``), keeping
only FRESH corrections — the superseding document fetched within the recency
window — whose value actually changed. ``should_fire`` gates on materiality
(``>= _MATERIAL_DELTA_PCT``). The memo is fully DETERMINISTIC — a restatement is a
factual "was X, now Y" event, so there is no LLM call to fail; the alert fires
whenever the data says it should. Re-fire dedup keys on the specific superseding
row, so a given correction pushes exactly once.

Failure modes: ``scan`` is best-effort (missing roster / facts tables → ``[]``,
never raises); ``build_alert`` is pure string assembly and never raises.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from alerts.store import compute_signature_sha
from identity import DEFAULT_USER_ID
from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    TriggerCandidate,
    UserStateContext,
)

log = logging.getLogger(__name__)

_USER_ID = DEFAULT_USER_ID

# Only push corrections whose superseding document landed within this window —
# the owner cares about NEW restatements, not re-litigating every historical
# 10-K-vs-10-Q reclassification. Combined with signature dedup, this keeps the
# first run from flooding the inbox with the whole back-catalogue.
_RECENCY_DAYS = 30

# A restated value must move the figure by at least this percent to fire. Below
# it the change is a rounding-level reclassification, not a thesis-relevant
# correction. (The credibility ledger's immaterial tolerance is 0.5%; the push
# bar is deliberately higher.) Raised 1.0 -> 3.0 (owner feedback 2026-07-14:
# pending-alert quality was low — a 1% restatement is noise, not a signal).
_MATERIAL_DELTA_PCT = 3.0

# Defensive cap on candidates emitted per ticker per run — dedup already
# suppresses re-fires, but this bounds a pathological backfill.
_MAX_CANDIDATES = 25

_ACTION_THESIS_UPDATE = "thesis_update"


class RestatementTrigger:
    """Daily sensor over supersede chains for held names."""

    kind: ClassVar[str] = "restatement"
    cadence: ClassVar[Cadence] = Cadence.DAILY

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]:
        """Emit one candidate per fresh, value-changed supersede of a held name.

        Returns ``[]`` when the ticker isn't a portfolio holding, the fact tables
        are absent, or nothing was restated recently — never raises.
        """
        if not _is_held(db, ticker):
            return []
        now = datetime.now(UTC).replace(tzinfo=None)
        cutoff = (now - timedelta(days=_RECENCY_DAYS)).isoformat(sep=" ")
        candidates: list[TriggerCandidate] = []
        for fact_table in ("financial_facts", "kpi_facts"):
            for ev in _recent_supersedes(db, ticker=ticker, fact_table=fact_table, cutoff=cutoff):
                candidates.append(
                    TriggerCandidate(
                        ticker=ticker.upper(),
                        kind=self.kind,
                        key=f"{ticker.upper()}:{fact_table}:{ev['new_fact_id']}",
                        evidence=ev,
                        computed_at=now,
                    )
                )
                if len(candidates) >= _MAX_CANDIDATES:
                    return candidates
        return candidates

    def should_fire(self, candidate: TriggerCandidate, user_state: UserStateContext) -> bool:
        """Fire on a material delta. Driver-side dedup owns dismissed-alert
        suppression, so the dismissed set isn't re-checked here."""
        _ = user_state
        delta = candidate.evidence.get("delta_pct")
        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
            return False
        return abs(float(delta)) >= _MATERIAL_DELTA_PCT

    def signature_key_evidence(self, candidate: TriggerCandidate) -> Mapping[str, object]:
        """Key the dedup hash on the specific superseding row — a given
        correction fires once, the next quarter's restatement fires fresh."""
        return {
            "fact_table": candidate.evidence["fact_table"],
            "new_fact_id": candidate.evidence["new_fact_id"],
        }

    def build_alert(self, candidate: TriggerCandidate, anchor: ThesisAnchor | None) -> AlertDraft:
        """Assemble the deterministic restatement alert. Never raises."""
        _ = anchor
        ticker = candidate.ticker
        evidence = dict(candidate.evidence)
        memo_text = _memo(ticker, evidence)
        evidence_json = json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str)
        signature_sha = compute_signature_sha(
            self.kind, ticker, self.signature_key_evidence(candidate)
        )
        return AlertDraft(
            trigger_kind=self.kind,
            ticker=ticker,
            fired_at=datetime.now(UTC).replace(tzinfo=None),
            evidence_json=evidence_json,
            signature_sha=signature_sha,
            memo_text=memo_text,
        )

    def draft_actions(
        self, alert: AlertDraft, candidate: TriggerCandidate
    ) -> list[QueuedActionDraft]:
        """A single thesis_update: a held-name number you may rely on moved —
        re-check the thesis against the corrected figure."""
        evidence = candidate.evidence
        item = evidence.get("line_item")
        if not isinstance(item, str):
            return []
        return [
            QueuedActionDraft(
                action_kind=_ACTION_THESIS_UPDATE,
                payload={
                    "body": (
                        f"RESTATEMENT - {alert.memo_text} Re-check any thesis assumption that "
                        f"rests on {item}."
                    ),
                    "line_item": item,
                    "restatement": True,
                },
            )
        ]


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def _is_held(conn: sqlite3.Connection, ticker: str) -> bool:
    """True when ``ticker`` is a current portfolio holding.

    Permissive when the ``tracked_companies`` roster is absent (synthetic
    fixtures / a fresh repo) — better to surface a restatement than to silently
    drop it; production always has the roster, where the gate is real.
    """
    try:
        has_roster = (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tracked_companies'"
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return True
    if not has_roster:
        return True
    try:
        row = conn.execute(
            "SELECT 1 FROM tracked_companies "
            "WHERE UPPER(ticker) = ? AND list_type = 'portfolio' AND archived_at IS NULL LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    except sqlite3.Error:
        return True
    return row is not None


def _recent_supersedes(
    conn: sqlite3.Connection, *, ticker: str, fact_table: str, cutoff: str
) -> list[dict[str, object]]:
    """Evidence dicts for value-changed supersedes of ``ticker`` whose
    superseding document was fetched on/after ``cutoff``. ``[]`` on any missing
    table / column (pre-0054 schemas)."""
    if not _has_supersedes(conn, fact_table):
        return []
    doc_cols = _columns(conn, "documents")
    has_identity = "accession_number" in doc_cols
    identity = (
        "d.accession_number AS accession_number, d.filing_date AS filing_date"
        if has_identity
        else "NULL AS accession_number, NULL AS filing_date"
    )
    if fact_table == "financial_facts":
        item_select = "new.line_item AS item"
        item_join = ""
    else:
        item_select = "kd.name AS item"
        item_join = "JOIN kpi_definitions kd ON kd.id = new.kpi_definition_id"
    sql = f"""
        SELECT new.id AS new_id, {item_select}, new.period_end AS period_end,
               new.fiscal_period_type AS fpt,
               CAST(old.value AS REAL) AS old_value, CAST(new.value AS REAL) AS new_value,
               old.source_doc_id AS old_doc_id, new.source_doc_id AS new_doc_id,
               d.doc_type AS new_doc_type, d.fetched_at AS new_fetched_at, {identity}
        FROM {fact_table} new
        JOIN {fact_table} old ON old.id = new.supersedes_id
        {item_join}
        LEFT JOIN documents d ON d.id = new.source_doc_id
        WHERE new.ticker = ?
          AND CAST(new.value AS REAL) != CAST(old.value AS REAL)
          AND (d.fetched_at IS NULL OR d.fetched_at >= ?)
        ORDER BY new.id DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (ticker.upper(), cutoff, _MAX_CANDIDATES)).fetchall()
    except sqlite3.Error as exc:
        log.debug(
            {"event": "restatement_scan_query_failed", "table": fact_table, "error": str(exc)}
        )
        return []
    out: list[dict[str, object]] = []
    for r in rows:
        old_value = None if r["old_value"] is None else float(r["old_value"])
        new_value = None if r["new_value"] is None else float(r["new_value"])
        out.append(
            {
                "fact_table": fact_table,
                "new_fact_id": int(r["new_id"]),
                "line_item": str(r["item"]),
                "period_end": str(r["period_end"])[:10],
                "fiscal_period_type": str(r["fpt"]),
                "old_value": old_value,
                "new_value": new_value,
                "delta_pct": _delta_pct(old_value, new_value),
                "old_doc_id": int(r["old_doc_id"]),
                "new_doc_id": int(r["new_doc_id"]),
                "new_doc_type": None if r["new_doc_type"] is None else str(r["new_doc_type"]),
                "new_accession": (
                    None if r["accession_number"] is None else str(r["accession_number"])
                ),
                "new_filing_date": None if r["filing_date"] is None else str(r["filing_date"]),
            }
        )
    return out


def _has_supersedes(conn: sqlite3.Connection, table: str) -> bool:
    return "supersedes_id" in _columns(conn, table)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _delta_pct(old: float | None, new: float | None) -> float | None:
    if old is None or new is None or old == 0.0:
        return None
    return abs(new - old) / abs(old) * 100.0


# --------------------------------------------------------------------------
# Deterministic memo
# --------------------------------------------------------------------------


def _fmt(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1_000_000_000:
        return f"${v / 1_000_000_000:,.2f}B"
    if a >= 1_000_000:
        return f"${v / 1_000_000:,.1f}M"
    if a >= 1_000:
        return f"${v:,.0f}"
    return f"{v:,.2f}"


def _memo(ticker: str, evidence: dict[str, object]) -> str:
    """Deterministic 'was X, now Y' memo for a held-name restatement."""
    item = str(evidence.get("line_item", "a figure"))
    period = _period_label(evidence)
    old_value = _as_float(evidence.get("old_value"))
    new_value = _as_float(evidence.get("new_value"))
    delta = evidence.get("delta_pct")
    direction = "up" if (new_value or 0) > (old_value or 0) else "down"
    pct = (
        f" ({direction} {abs(float(delta)):.1f}%)"
        if isinstance(delta, (int, float)) and not isinstance(delta, bool)
        else ""
    )
    filing = _filing_label(evidence)
    return (
        f"{ticker} {item} for {period} was restated from {_fmt(old_value)} to "
        f"{_fmt(new_value)}{pct}{filing}. A held-name number your thesis may rest on was "
        "corrected."
    )


def _period_label(evidence: dict[str, object]) -> str:
    fpt = str(evidence.get("fiscal_period_type", ""))
    period_end = str(evidence.get("period_end", ""))
    year = period_end[:4] if len(period_end) >= 4 else period_end
    return f"{fpt} {year}".strip()


def _filing_label(evidence: dict[str, object]) -> str:
    doc_type = evidence.get("new_doc_type")
    filing_date = evidence.get("new_filing_date")
    if isinstance(doc_type, str) and doc_type:
        bit = f" by a later {doc_type} filing"
        if isinstance(filing_date, str) and filing_date:
            bit += f" (filed {filing_date[:10]})"
        return bit
    return ""


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None

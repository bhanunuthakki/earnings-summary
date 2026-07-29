"""Extract LLM recommendations from `lens:five_min_reread` artifacts.

The five-min-reread lens emits a "Recommended action" section with one of
ADD <N>% / TRIM <N>% / HOLD / SELL — the user-facing capital-allocation
verdict. Week 4 of the fresh-review memo turns those into a durable
audit ledger:

  extract_recommendations_from_artifact(artifact) -> list[DecisionCandidate]
      Pure-function parse. Regex first (covers 95% of clean outputs);
      LLM (Haiku) fallback only when the markdown shape is ambiguous or
      the regex returns nothing despite the section being present.

  record_decisions_from_artifacts(repo_root, since_days=30) -> int
      Walks recent llm_artifacts of purpose='lens:five_min_reread', extracts
      decisions, upserts into the `decisions` table. Idempotent on
      source_artifact_id — re-running on the same artifact is a no-op.

  record_user_action(decision_id, action, notes, db_path) -> bool
  record_outcome(decision_id, outcome_label, outcome_pct, ..., db_path) -> bool
      Write paths for the user-action and grading sides.

  history(ticker, db_path, limit) -> list[Decision]
  outcome_curve_by_conviction(db_path, since_days) -> dict
      Read paths used by the calibration lens and dashboard panel.

Idempotency: enforced by a UNIQUE index on `source_artifact_id`. A second
record attempt on the same artifact returns the existing row's id without
inserting. When the LLM regenerates a five-min-reread (new artifact id via
supersession), a new decisions row is written — the prior decision keeps its
outcome history, since the user already acted on it.

Module is best-effort against missing DB / missing table — matches the
predictions_store pattern. The lens that emitted the artifact does not
depend on this module; this is downstream-only.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from model_provenance.basis import Basis, dcf_basis
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

if TYPE_CHECKING:
    from integrations.portfolio_tracker_client import LivePortfolio

from db_paths import resolve_db_path

log = logging.getLogger(__name__)


RecommendationKind = Literal["add", "trim", "hold", "sell", "initiate", "avoid"]
UserAction = Literal["followed", "ignored", "partial", "reversed"]
OutcomeLabel = Literal["correct", "wrong", "mixed", "unfalsifiable", "pending"]
# Process-quality is a SEPARATE axis from outcome (Track B seam 8): a 'sound'
# process can still grade 'wrong' (wrong for the right reasons) and a 'lucky' one
# can grade 'correct' (right for the wrong reasons).
ProcessQuality = Literal["sound", "flawed", "lucky"]
PROCESS_QUALITY_VOCAB: frozenset[str] = frozenset({"sound", "flawed", "lucky"})

# Recognized recommendation kinds. Order matters for the regex alternation —
# put longer literals first so "INITIATE" wins over "INIT" if someone writes
# both. The set also acts as the validation gate.
_KIND_VOCAB: tuple[str, ...] = ("initiate", "avoid", "trim", "hold", "sell", "add")

# Matches **ADD 8%** / **TRIM 20%** / **HOLD** / **HOLD.** / **SELL** / etc.
# Tolerates a trailing period or space inside the bold markers. The size is
# optional and only meaningful for add/trim/initiate. Case-insensitive.
_RECO_RX = re.compile(
    r"\*\*\s*(?P<kind>ADD|TRIM|HOLD|SELL|INITIATE|AVOID)"
    r"(?:\s+(?P<value>\d+(?:\.\d+)?)\s*%)?"
    r"\s*\.?\s*\*\*",
    re.IGNORECASE,
)

# Matches the section header for the recommended-action paragraph. The
# five_min_reread lens emits "## 2. Recommended action" but some outputs
# drop the numbering or capitalize differently — accept any of these.
_SECTION_RX = re.compile(
    r"##\s*(?:\d+\.\s*)?Recommended\s+Action\s*\n+(?P<body>.*?)(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# Conviction-language probe. The recommended-action paragraph itself doesn't
# carry an explicit conviction label, but the surrounding text often does.
# Conservative pattern — only flips conviction when the language is loud.
_CONVICTION_RX = re.compile(
    r"\b(high\s+conviction|low\s+conviction|medium\s+conviction|tier-1|asymmetric|speculative)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DecisionCandidate:
    """One extracted recommendation, pre-write.

    `value` is the position-size pct when kind ∈ {add, trim, initiate}; None
    for hold/sell/avoid. `conviction` is best-effort heuristic, None when
    nothing in the markdown signals it loudly enough.
    """

    kind: RecommendationKind
    value: float | None
    conviction: str | None
    rationale_excerpt: str
    source_lens: str


@dataclass(slots=True)
class Decision:
    """One row from the `decisions` table as the public API sees it."""

    id: int
    ticker: str
    recommendation_kind: str
    recommendation_value: float | None
    conviction: str | None
    # NULL for memo-sourced (0086) and pass/avoid (0110) rows — anchored by
    # source_memo_id / source_dismissal_id instead.
    source_artifact_id: int | None
    source_lens: str | None
    rationale_excerpt: str | None
    made_at: datetime
    user_acted_at: datetime | None
    user_action_kind: str | None
    user_notes: str | None
    outcome_at: datetime | None
    outcome_label: str | None
    outcome_pct: float | None
    outcome_notes: str | None
    # Process quality (Track B seam 8) — a separate axis from outcome. Defaulted
    # so a SELECT off a pre-0114 schema (no column) still builds the dataclass.
    process_quality: str | None = None


# ===========================================================================
# Extraction — pure functions, no I/O
# ===========================================================================


def extract_recommendations_from_artifact(
    *,
    content_md: str | None,
    source_lens: str = "five_min_reread",
    llm_fallback: bool = False,
    ticker: str | None = None,
) -> list[DecisionCandidate]:
    """Parse a five-min-reread artifact's markdown body into a list of
    DecisionCandidate. Returns [] when no recommendation can be extracted.

    Strategy:
      1. Locate the "Recommended action" section via _SECTION_RX. If absent,
         the artifact didn't follow the lens schema — return [] (no LLM
         fallback fires for this case, since the artifact is structurally
         off-prompt and the LLM would just hallucinate a verdict).
      2. Apply _RECO_RX to that section. The five-min-reread prompt asks for
         ONE recommendation, so we return the first match; multiple matches
         are unusual but possible (e.g. "ADD 8% (flip to TRIM if ...)") —
         the FIRST is the operative verdict.
      3. Conviction is harvested from the surrounding paragraph via
         _CONVICTION_RX. Best-effort, often None.
      4. If `llm_fallback=True` and the regex extracted nothing despite a
         present section, fire the Haiku fallback. Off by default — the
         caller turns it on for re-runs where a noisier shape is expected.
    """
    if not content_md:
        return []

    section_match = _SECTION_RX.search(content_md)
    if section_match is None:
        return []
    body = section_match.group("body").strip()
    if not body:
        return []

    reco_match = _RECO_RX.search(body)
    if reco_match is not None:
        kind = reco_match.group("kind").lower()
        raw_value = reco_match.group("value")
        value = float(raw_value) if raw_value is not None else None
        if kind not in _KIND_VOCAB:
            return []
        conviction = _extract_conviction(body)
        rationale = _excerpt_after_recommendation(body, reco_match.end())
        return [
            DecisionCandidate(
                kind=cast("RecommendationKind", kind),
                value=value,
                conviction=conviction,
                rationale_excerpt=rationale,
                source_lens=source_lens,
            )
        ]

    if llm_fallback and ticker is not None:
        return _llm_fallback_extract(body=body, ticker=ticker, source_lens=source_lens)

    return []


def _extract_conviction(body: str) -> str | None:
    """Heuristic conviction read. Returns 'high' | 'medium' | 'low' | None."""
    match = _CONVICTION_RX.search(body)
    if match is None:
        return None
    label = match.group(1).lower()
    if "high" in label or "asymmetric" in label or "tier-1" in label:
        return "high"
    if "low" in label or "speculative" in label:
        return "low"
    if "medium" in label:
        return "medium"
    return None


def _excerpt_after_recommendation(body: str, start_offset: int) -> str:
    """Trim the post-verdict justification down to a one-paragraph excerpt
    suitable for the rationale_excerpt column. ~400 chars cap."""
    tail = body[start_offset:].strip()
    # Stop at the first blank line so we don't pull in the "what would change
    # my mind" section's preamble.
    para = tail.split("\n\n", 1)[0].strip()
    return para[:512]


def _llm_fallback_extract(*, body: str, ticker: str, source_lens: str) -> list[DecisionCandidate]:
    """Haiku-fallback for the rare case where the regex misses a present
    recommendation. Returns at most one candidate. Defensive against
    JSON-decoding failures and unknown kinds."""
    try:
        from llm_client import call_llm
    except ImportError:
        log.debug({"event": "decision_extractor_llm_unavailable"})
        return []

    prompt = f"""You are a strict JSON extractor. The following markdown is a
"Recommended action" paragraph from an analyst memo for {ticker}. Extract the
single operative recommendation. Return ONLY a JSON object, no commentary:

{{
  "kind": "add" | "trim" | "hold" | "sell" | "initiate" | "avoid",
  "value": <number or null>,    // position-size pct when add/trim/initiate; null otherwise
  "conviction": "low" | "medium" | "high" | null
}}

If the paragraph contains no extractable recommendation, return:
{{"kind": null}}

Paragraph:
{body[:2000]}
"""

    try:
        # Model resolves from LLM_MODELS["decision_extraction"] — the registry
        # is the single reviewable surface for pins (llm_evals_plan.md §5.5).
        raw = call_llm(
            prompt,
            purpose="decision_extraction",
            ticker=ticker,
        )
    except Exception as exc:  # extractor is best-effort
        log.warning({"event": "decision_extractor_llm_failed", "error": str(exc)})
        return []

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        log.warning({"event": "decision_extractor_llm_parse_failed", "raw_head": raw[:200]})
        return []
    if not isinstance(decoded, dict):
        return []
    d = cast("dict[str, object]", decoded)
    kind_raw = d.get("kind")
    if not isinstance(kind_raw, str):
        return []
    kind = kind_raw.lower()
    if kind not in _KIND_VOCAB:
        return []
    value: float | None = None
    raw_value = d.get("value")
    if isinstance(raw_value, (int, float)):
        value = float(raw_value)
    conviction_raw = d.get("conviction")
    conviction = (
        conviction_raw
        if isinstance(conviction_raw, str) and conviction_raw in {"low", "medium", "high"}
        else None
    )
    return [
        DecisionCandidate(
            kind=cast("RecommendationKind", kind),
            value=value,
            conviction=conviction,
            rationale_excerpt=body.strip()[:512],
            source_lens=source_lens,
        )
    ]


# ===========================================================================
# DB — open, record, read
# ===========================================================================


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    try:
        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = connect_sqlite(
            path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()
            is None
        ):
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _has_basis_columns(conn: sqlite3.Connection) -> bool:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(decisions)")}
    return "basis_kind" in cols


def record_decision(
    *,
    ticker: str,
    recommendation_kind: RecommendationKind,
    recommendation_value: float | None,
    conviction: str | None,
    source_artifact_id: int,
    source_lens: str | None,
    rationale_excerpt: str | None,
    made_at: datetime,
    basis: Basis | None = None,
    resolve_dcf_basis: bool = True,
    db_path: Path | str | None = None,
) -> int | None:
    """Idempotent insert. Returns the row id (existing or new) or None on
    DB unavailability. Idempotency is by source_artifact_id via the UNIQUE
    index — second insert on the same artifact returns the existing row.

    Valuation basis (migration 0137): the recommendation records which model-version
    it rests on so it can be flagged stale when a newer model supersedes it. Pass an
    explicit ``basis``; otherwise, when ``resolve_dcf_basis`` is set, the current DCF
    fair value for the ticker is captured automatically. No-ops cleanly on a pre-0137
    schema (basis columns absent) — the row is written without a basis.
    """
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        existing = conn.execute(
            "SELECT id FROM decisions WHERE source_artifact_id = ?",
            (source_artifact_id,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        if basis is None and resolve_dcf_basis:
            basis = dcf_basis(conn, ticker)

        cols = [
            "ticker",
            "recommendation_kind",
            "recommendation_value",
            "conviction",
            "source_artifact_id",
            "source_lens",
            "rationale_excerpt",
            "made_at",
            "outcome_label",
            "created_at",
        ]
        vals: list[object] = [
            ticker.upper(),
            recommendation_kind,
            recommendation_value,
            conviction,
            source_artifact_id,
            source_lens,
            rationale_excerpt,
            made_at.isoformat(),
            "pending",
            datetime.now(UTC).isoformat(),
        ]
        if basis is not None and _has_basis_columns(conn):
            cols += ["basis_kind", "basis_ref_id", "basis_value", "basis_as_of", "basis_meta_json"]
            vals += [basis.kind, basis.ref_id, basis.value, basis.as_of, basis.meta_json]

        placeholders = ",".join("?" * len(vals))
        cur = conn.execute(
            f"INSERT INTO decisions({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "decision_record_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def record_user_action(
    *,
    decision_id: int,
    user_action_kind: UserAction,
    user_notes: str | None = None,
    user_acted_at: datetime | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Record the user's response to a recommendation. Idempotent — repeated
    calls overwrite. Returns True on success."""
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        conn.execute(
            """
            UPDATE decisions
            SET user_acted_at = ?, user_action_kind = ?, user_notes = ?
            WHERE id = ?
            """,
            (
                (user_acted_at or datetime.now(UTC)).isoformat(),
                user_action_kind,
                user_notes,
                decision_id,
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        log.warning({"event": "decision_user_action_failed", "error": str(exc)})
        return False
    finally:
        conn.close()


def record_outcome(
    *,
    decision_id: int,
    outcome_label: OutcomeLabel,
    outcome_pct: float | None = None,
    outcome_notes: str | None = None,
    outcome_at: datetime | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Record a graded outcome. Idempotent. Returns True on success."""
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        conn.execute(
            """
            UPDATE decisions
            SET outcome_at = ?, outcome_label = ?, outcome_pct = ?, outcome_notes = ?
            WHERE id = ?
            """,
            (
                (outcome_at or datetime.now(UTC)).isoformat(),
                outcome_label,
                outcome_pct,
                outcome_notes,
                decision_id,
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        log.warning({"event": "decision_outcome_failed", "error": str(exc)})
        return False
    finally:
        conn.close()


def record_process_quality(
    *,
    decision_id: int,
    process_quality: ProcessQuality,
    db_path: Path | str | None = None,
) -> bool:
    """Score a decision's PROCESS quality (sound / flawed / lucky) — the axis
    distinct from its outcome (Track B seam 8). Idempotent — repeated calls
    overwrite. Raises ``ValueError`` on an unknown label; returns False on DB
    unavailability (best-effort, like the other write paths)."""
    if process_quality not in PROCESS_QUALITY_VOCAB:
        raise ValueError(
            f"unknown process_quality {process_quality!r}; "
            f"expected one of {sorted(PROCESS_QUALITY_VOCAB)}"
        )
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        conn.execute(
            "UPDATE decisions SET process_quality = ? WHERE id = ?",
            (process_quality, decision_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        log.warning({"event": "decision_process_quality_failed", "error": str(exc)})
        return False
    finally:
        conn.close()


def history(
    *,
    ticker: str | None = None,
    limit: int = 200,
    since_days: int | None = None,
    db_path: Path | str | None = None,
) -> list[Decision]:
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        clauses: list[str] = []
        params: list[object] = []
        if ticker is not None:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if since_days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
            clauses.append("made_at >= ?")
            params.append(cutoff)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT * FROM decisions
            {where}
            ORDER BY made_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row_to_decision(r) for r in rows]
    finally:
        conn.close()


# Recommendation kind → the tracker transaction direction it implies. HOLD /
# AVOID imply NO trade (inaction is the action), so they're absent here and
# handled separately in the reconciler.
_KIND_DIRECTION: dict[str, str] = {
    "add": "buy",
    "initiate": "buy",
    "trim": "sell",
    "sell": "sell",
}


def _date_prefix(raw: object) -> date | None:
    """Parse the YYYY-MM-DD prefix of a transaction stamp; None on bad input."""
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def reconcile_decision_actions(
    *,
    db_path: Path | str | None = None,
    portfolio: LivePortfolio | None = None,
    window_days: int = 30,
    lookback_days: int = 180,
    transactions_limit: int = 200,
    now: datetime | None = None,
) -> dict[str, int]:
    """Match every decision with NO recorded user action to the tracker's
    subsequent fills (same ticker, the direction the call implies, within
    ``[made_at, made_at + window_days]``) and write ``user_action_kind`` — the
    decision→fill link that has had no production caller, leaving calibration's
    ``action_mix`` structurally empty.

      'followed'  — a matching-direction fill landed in the window.
      'reversed'  — only the OPPOSITE-direction fill landed (you did the contrary).
      'partial'   — a HOLD / AVOID that nonetheless traded (you didn't fully
                    hold / pass).
      'ignored'   — no fill AND the window has fully elapsed (the call lapsed).

    Undetermined calls (no fill yet, window still open) are left NULL and
    revisited next run. Idempotent: only NULL-action rows are touched, and a
    second run against the same world is a no-op. Best-effort: a missing DB or an
    offline tracker returns a tally with the reason flagged, never raises."""
    tally = {
        k: 0
        for k in ("followed", "reversed", "partial", "ignored", "skipped_undetermined", "scanned")
    }
    tally["db_unavailable"] = 0
    tally["tracker_unavailable"] = 0

    now_naive = (now or datetime.now(UTC)).replace(tzinfo=None)
    conn = _open(db_path)
    if conn is None:
        tally["db_unavailable"] = 1
        return tally
    try:
        cutoff = (now_naive - timedelta(days=lookback_days)).isoformat()
        rows = conn.execute(
            "SELECT id, ticker, recommendation_kind, made_at FROM decisions "
            "WHERE user_action_kind IS NULL AND made_at >= ? ORDER BY made_at DESC",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    # No early return on empty `rows` -- the tracker-fill draft pass below
    # must still run even with zero NULL-action decisions.

    if portfolio is None:
        try:
            from integrations.portfolio_tracker_client import fetch_live_portfolio

            portfolio = fetch_live_portfolio(transactions_limit=transactions_limit)
        except Exception:  # tracker import / call is best-effort
            tally["tracker_unavailable"] = 1
            return tally
    if not portfolio.available:
        tally["tracker_unavailable"] = 1
        return tally

    fills_by_ticker: dict[str, list[tuple[date, str]]] = {}
    for txn in portfolio.transactions:
        tk = (txn.ticker or "").upper()
        direction = txn.type.lower()
        day = _date_prefix(txn.date)
        if tk and direction in ("buy", "sell") and day is not None:
            fills_by_ticker.setdefault(tk, []).append((day, direction))

    for row in rows:
        tally["scanned"] += 1
        ticker = str(row["ticker"] or "").upper()
        made_dt = _parse_dt(row["made_at"])
        if not ticker or made_dt is None:
            tally["skipped_undetermined"] += 1
            continue
        made_dt = made_dt.replace(tzinfo=None)
        window_end = made_dt + timedelta(days=window_days)
        window_elapsed = now_naive > window_end
        in_window = [
            (day, direction)
            for day, direction in fills_by_ticker.get(ticker, [])
            if made_dt.date() <= day <= window_end.date()
        ]
        has_buy = any(direction == "buy" for _, direction in in_window)
        has_sell = any(direction == "sell" for _, direction in in_window)
        kind = str(row["recommendation_kind"] or "").lower()
        direction = _KIND_DIRECTION.get(kind)
        action: str | None = None
        fill_day: date | None = None
        if direction is not None:
            opposite = "sell" if direction == "buy" else "buy"
            matched = has_buy if direction == "buy" else has_sell
            opposed = has_sell if direction == "buy" else has_buy
            if matched:
                action = "followed"
                fill_day = min(d for d, dr in in_window if dr == direction)
            elif opposed:
                action = "reversed"
                fill_day = min(d for d, dr in in_window if dr == opposite)
            elif window_elapsed:
                action = "ignored"
        elif has_buy or has_sell:  # HOLD / AVOID that nonetheless traded
            action = "partial"
            fill_day = min(d for d, _ in in_window)
        elif window_elapsed:  # held / passed cleanly through the window
            action = "followed"

        if action is None:
            tally["skipped_undetermined"] += 1
            continue
        acted_at = (
            datetime.combine(fill_day, datetime.min.time()) if fill_day is not None else window_end
        )
        ok = record_user_action(
            decision_id=int(row["id"]),
            user_action_kind=action,
            user_notes=f"auto-reconciled from tracker fills ({window_days}d window)",
            user_acted_at=acted_at,
            db_path=db_path,
        )
        tally[action if ok else "skipped_undetermined"] += 1

    tally.update(
        _draft_unmatched_fills(portfolio.transactions, window_days=window_days, db_path=db_path)
    )
    return tally


def _fill_identity_key(
    ticker: str,
    day: date,
    direction: str,
    quantity: float | None,
    amount: float | None,
    transaction_id: str | None = None,
) -> str:
    """Deterministic idempotency key for one tracker fill (PRD §13.1 "capture
    coverage" measure) — ticker+date+type+quantity+amount, hashed so a
    same-day duplicate fill (a split buy) still gets a distinct key when its
    size differs, and re-running the sweep never redrafts the same fill."""
    import hashlib

    if transaction_id:
        raw = f"id|{transaction_id}"
        prefix = "tracker-id:"
    else:
        raw = f"{ticker}|{day.isoformat()}|{direction}|{quantity}|{amount}"
        prefix = "tracker:"
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _reconcile_provider_fill_identity(
    conn: sqlite3.Connection,
    *,
    provider_key: str,
    legacy_key: str,
    provider_transaction_id: str,
) -> bool:
    """Adopt a legacy signature row or archive it behind its V1 identity twin.

    Returns true when a provider-identity row already represents this fill.
    Missing/pre-0195 draft schema degrades to false so the existing best-effort
    writer path retains its behavior.
    """
    try:
        # Identity state and any state transfer/archival are one serialized
        # unit. Without the reserved writer lock, an Inbox action could commit
        # after these reads and be overwritten by a stale reconciliation
        # snapshot.
        conn.execute("BEGIN IMMEDIATE")
        provider_row = conn.execute(
            "SELECT id, status, decision_id, confirmed_at, dismissed_at, draft_json, "
            "parse_confidence FROM decision_drafts WHERE idempotency_key = ?",
            (provider_key,),
        ).fetchone()
        legacy_row = conn.execute(
            "SELECT id, status, decision_id, confirmed_at, dismissed_at, draft_json, "
            "parse_confidence FROM decision_drafts WHERE idempotency_key = ?",
            (legacy_key,),
        ).fetchone()
        if provider_row is None and legacy_row is not None:
            conn.execute(
                "UPDATE decision_drafts SET idempotency_key = ?, source_provider_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (provider_key, provider_transaction_id, int(legacy_row[0])),
            )
            conn.commit()
            return True
        if provider_row is None:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE decision_drafts SET source_provider_id = COALESCE(source_provider_id, ?), "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (provider_transaction_id, int(provider_row[0])),
        )
        if legacy_row is not None and int(legacy_row[0]) != int(provider_row[0]):
            legacy_status = str(legacy_row[1])
            provider_status = str(provider_row[1])
            legacy_decision_id = legacy_row[2]
            provider_decision_id = provider_row[2]
            legacy_is_decision = (
                legacy_status in {"confirmed", "corrected"} and legacy_decision_id is not None
            )
            provider_is_decision = (
                provider_status in {"confirmed", "corrected"} and provider_decision_id is not None
            )
            if (
                legacy_is_decision
                and provider_is_decision
                and legacy_decision_id is not None
                and provider_decision_id is not None
                and int(legacy_decision_id) != int(provider_decision_id)
            ):
                # Never erase either side of a conflicting, decision-linked
                # identity pair. Mark both decisions durably so the conflict is
                # visible in the journal even when no future late fill causes
                # the group action core to raise.
                conflict_ids = sorted((int(provider_decision_id), int(legacy_decision_id)))
                conflict_marker = f"tracker_identity_conflict:{conflict_ids[0]}:{conflict_ids[1]}"
                conflict_note = (
                    "\n\n---\n"
                    f"[{conflict_marker}] "
                    "Tracker identity conflict: provider and legacy fill rows "
                    f"link different decisions ({provider_decision_id}, "
                    f"{legacy_decision_id}). Manual review required."
                )
                conn.execute(
                    "UPDATE decisions SET user_notes = COALESCE(user_notes, '') || ? "
                    "WHERE id IN (?, ?) AND instr(COALESCE(user_notes, ''), ?) = 0",
                    (
                        conflict_note,
                        provider_decision_id,
                        legacy_decision_id,
                        conflict_marker,
                    ),
                )
                log.error(
                    {
                        "event": "tracker_identity_decision_conflict",
                        "provider_key": provider_key,
                        "legacy_key": legacy_key,
                        "provider_decision_id": provider_decision_id,
                        "legacy_decision_id": legacy_decision_id,
                    }
                )
                conn.commit()
                return True
            if legacy_is_decision and not provider_is_decision:
                conn.execute(
                    "UPDATE decision_drafts SET status = ?, decision_id = ?, confirmed_at = ?, "
                    "dismissed_at = ?, draft_json = ?, parse_confidence = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (
                        legacy_status,
                        legacy_row[2],
                        legacy_row[3],
                        legacy_row[4],
                        legacy_row[5],
                        legacy_row[6],
                        int(provider_row[0]),
                    ),
                )
                provider_status = legacy_status
                provider_is_decision = True
            elif (
                legacy_status not in {"awaiting_confirmation", "expired"}
                and provider_status == "awaiting_confirmation"
            ):
                conn.execute(
                    "UPDATE decision_drafts SET status = ?, decision_id = ?, confirmed_at = ?, "
                    "dismissed_at = ?, draft_json = ?, parse_confidence = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (
                        legacy_status,
                        legacy_row[2],
                        legacy_row[3],
                        legacy_row[4],
                        legacy_row[5],
                        legacy_row[6],
                        int(provider_row[0]),
                    ),
                )
            conn.execute(
                "UPDATE decision_drafts SET status = 'expired', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(legacy_row[0]),),
            )
        conn.commit()
        return True
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return False


def _draft_unmatched_fills(
    transactions: object,
    *,
    window_days: int,
    db_path: Path | str | None,
) -> dict[str, int]:
    """PRD §13.1's capture-coverage gap: a tracker fill with NO ``decisions``
    row for that ticker anywhere in a ``window_days`` lookback around it was
    silently dropped by the reconciliation above (it only ever touches
    NULL-action decision rows, never fills that have no candidate decision at
    all). Every such fill gets an explicit, confirmable
    ``decision_drafts`` row (source_channel='tracker') instead — the
    "confirmable draft or explicit unmatched status" the measure requires.
    Idempotent via :func:`_fill_identity_key`; best-effort against a missing
    decision_drafts table (pre-0195 DB)."""
    counts = {"tracker_fill_matched": 0, "tracker_fill_drafted": 0, "tracker_fill_draft_failed": 0}
    conn = _open(db_path)
    if conn is None:
        return counts
    seen_keys: set[str] = set()
    try:
        for txn in cast("list[object]", transactions):
            ticker = str(getattr(txn, "ticker", None) or "").upper()
            direction = str(getattr(txn, "type", "") or "").lower()
            day = _date_prefix(getattr(txn, "date", None))
            if not ticker or direction not in ("buy", "sell") or day is None:
                continue
            transaction_id = getattr(txn, "transaction_id", None)
            key = _fill_identity_key(
                ticker,
                day,
                direction,
                getattr(txn, "quantity", None),
                getattr(txn, "amount", None),
                transaction_id,
            )
            if key in seen_keys:
                continue  # a duplicate fill signature this run — already handled above
            seen_keys.add(key)
            if transaction_id:
                legacy_key = _fill_identity_key(
                    ticker,
                    day,
                    direction,
                    getattr(txn, "quantity", None),
                    getattr(txn, "amount", None),
                )
                if _reconcile_provider_fill_identity(
                    conn,
                    provider_key=key,
                    legacy_key=legacy_key,
                    provider_transaction_id=str(transaction_id),
                ):
                    counts["tracker_fill_drafted"] += 1
                    continue

            lo = (day - timedelta(days=window_days)).isoformat()
            hi = day.isoformat()
            source_external_id = f"{ticker}:{day.isoformat()}:{direction}"
            has_candidate = (
                conn.execute(
                    "SELECT 1 FROM decisions WHERE UPPER(ticker) = ? "
                    "AND substr(made_at, 1, 10) BETWEEN ? AND ? LIMIT 1",
                    (ticker, lo, hi),
                ).fetchone()
                is not None
            )
            if has_candidate:
                try:
                    has_confirmed_tracker_group = (
                        conn.execute(
                            "SELECT 1 FROM decision_drafts "
                            "WHERE source_channel = 'tracker' AND source_external_id = ? "
                            "AND decision_id IS NOT NULL LIMIT 1",
                            (source_external_id,),
                        ).fetchone()
                        is not None
                    )
                except sqlite3.Error:
                    has_confirmed_tracker_group = False
                has_candidate = not has_confirmed_tracker_group
            if has_candidate:
                counts["tracker_fill_matched"] += 1
                continue
            try:
                from capture.decision_draft import DecisionDraft, create_draft_row

                amount = getattr(txn, "amount", None)
                draft = DecisionDraft(
                    intent="executed_change",
                    proposed_ticker=ticker,
                    proposed_action=direction,
                    proposed_amount_usd=float(amount) if amount else None,
                    parse_confidence=1.0,
                )
                create_draft_row(
                    source_note_id=None,
                    source_channel="tracker",
                    source_external_id=source_external_id,
                    source_provider_id=(
                        str(transaction_id) if transaction_id is not None else None
                    ),
                    idempotency_key=key,
                    original_text=(
                        f"Tracker-detected {direction} fill: {ticker} on {day.isoformat()} "
                        "(no candidate decision on record)"
                    ),
                    draft=draft,
                    status="awaiting_confirmation",
                    db_path=db_path,
                )
                counts["tracker_fill_drafted"] += 1
            except Exception:  # a draft-write failure must not abort the reconcile sweep
                log.warning({"event": "tracker_fill_draft_failed", "ticker": ticker}, exc_info=True)
                counts["tracker_fill_draft_failed"] += 1
    finally:
        conn.close()
    return counts


def pending_for_grading(
    *,
    older_than_days: int = 30,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[Decision]:
    """Decisions where outcome_at IS NULL and made_at is older than threshold."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        rows = conn.execute(
            """
            SELECT * FROM decisions
            WHERE outcome_at IS NULL AND made_at <= ?
            ORDER BY made_at ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [_row_to_decision(r) for r in rows]
    finally:
        conn.close()


def hit_rate_by_kind(
    *,
    since_days: int = 365,
    db_path: Path | str | None = None,
) -> dict[str, dict[str, int]]:
    """Aggregate outcomes by recommendation_kind. Returns:
    {kind: {label: count, ...}, ...}. Used by the dashboard panel."""
    conn = _open(db_path)
    if conn is None:
        return {}
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        rows = conn.execute(
            """
            SELECT recommendation_kind, COALESCE(outcome_label, 'pending') AS outcome_label,
                   COUNT(*) AS n
            FROM decisions
            WHERE made_at >= ?
            GROUP BY recommendation_kind, outcome_label
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(str(r["recommendation_kind"]), {})[str(r["outcome_label"])] = int(r["n"])
    return out


def outcome_curve_by_conviction(
    *,
    since_days: int = 180,
    db_path: Path | str | None = None,
) -> dict[str, dict[str, int]]:
    """Calibration curve: stated conviction → outcome distribution. Used by
    the llm_calibration lens. Returns {conviction_bucket: {label: count}}."""
    conn = _open(db_path)
    if conn is None:
        return {}
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        rows = conn.execute(
            """
            SELECT COALESCE(conviction, 'unstated') AS conviction,
                   COALESCE(outcome_label, 'pending') AS outcome_label,
                   COUNT(*) AS n
            FROM decisions
            WHERE made_at >= ?
            GROUP BY conviction, outcome_label
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(str(r["conviction"]), {})[str(r["outcome_label"])] = int(r["n"])
    return out


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=int(row["id"]),
        ticker=row["ticker"],
        recommendation_kind=row["recommendation_kind"],
        recommendation_value=(
            float(row["recommendation_value"]) if row["recommendation_value"] is not None else None
        ),
        conviction=row["conviction"],
        source_artifact_id=(
            int(row["source_artifact_id"]) if row["source_artifact_id"] is not None else None
        ),
        source_lens=row["source_lens"],
        rationale_excerpt=row["rationale_excerpt"],
        made_at=_parse_dt(row["made_at"]) or datetime.now(UTC),
        user_acted_at=_parse_dt(row["user_acted_at"]),
        user_action_kind=row["user_action_kind"],
        user_notes=row["user_notes"],
        outcome_at=_parse_dt(row["outcome_at"]),
        outcome_label=row["outcome_label"],
        outcome_pct=float(row["outcome_pct"]) if row["outcome_pct"] is not None else None,
        outcome_notes=row["outcome_notes"],
        # sqlite3.Row membership tests VALUES, not keys — .keys() is required to
        # tolerate a SELECT off a pre-0114 schema that lacks the column.
        process_quality=(
            row["process_quality"] if "process_quality" in row.keys() else None  # noqa: SIM118
        ),
    )


def _parse_dt(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


# ===========================================================================
# Batch recorder — the workflow CLI's only entry point
# ===========================================================================


def record_decisions_from_artifacts(
    *,
    repo_root: Path,
    since_days: int = 30,
    source_lenses: tuple[str, ...] = ("lens:five_min_reread",),
    llm_fallback: bool = False,
    allowed_list_types: tuple[str, ...] = ("portfolio", "evaluation"),
) -> dict[str, int]:
    """Walk recent lens artifacts of the named purposes, extract decisions,
    upsert into the decisions table.

    Only tickers whose ``tracked_companies.list_type`` is in
    ``allowed_list_types`` are promoted: the decisions table is the
    calibration ledger, and a P3-tier (index_member / etf) lens brief is
    advisory reading, not a decision anyone will act on. Without this guard
    the monthly P3 sweep floods the ledger with no-context recommendations
    (2026-07-01: 40 index_member rows from an A→AIT alphabetical sweep).
    If the DB has no tracked_companies table (synthetic test DBs), the
    guard is skipped.

    Returns a tally: {inserted, skipped_existing, skipped_untracked,
    no_recommendation, db_unavailable}.
    """
    db_path = repo_root / "data" / "portfolio.db"
    tally = {
        "inserted": 0,
        "skipped_existing": 0,
        "skipped_untracked": 0,
        "no_recommendation": 0,
        "db_unavailable": 0,
    }
    if not db_path.exists():
        tally["db_unavailable"] = 1
        return tally

    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    conn.row_factory = sqlite3.Row
    try:
        # Decisions table existence check — gracefully handle pre-migration DB
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()
            is None
        ):
            tally["db_unavailable"] = 1
            return tally

        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        placeholders = ",".join("?" * len(source_lenses))
        rows = conn.execute(
            f"""
            SELECT id, ticker, purpose, content_md, generated_at
            FROM llm_artifacts
            WHERE purpose IN ({placeholders})
              AND superseded_by_id IS NULL
              AND generated_at >= ?
            ORDER BY generated_at DESC
            """,
            (*source_lenses, cutoff),
        ).fetchall()

        # Calibration-universe guard (see docstring). None = no table, skip guard.
        allowed_tickers: set[str] | None = None
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tracked_companies'"
            ).fetchone()
            is not None
        ):
            lt_placeholders = ",".join("?" * len(allowed_list_types))
            allowed_tickers = {
                str(r[0])
                for r in conn.execute(
                    f"SELECT ticker FROM tracked_companies WHERE list_type IN ({lt_placeholders})",
                    allowed_list_types,
                ).fetchall()
            }
    finally:
        conn.close()

    for row in rows:
        artifact_id = int(row["id"])
        ticker = row["ticker"]
        if not ticker:
            continue
        if allowed_tickers is not None and str(ticker) not in allowed_tickers:
            tally["skipped_untracked"] += 1
            continue
        # purpose has shape 'lens:<name>'; pass the name only to the candidate
        purpose = str(row["purpose"])
        lens_name = purpose.split(":", 1)[1] if ":" in purpose else purpose
        candidates = extract_recommendations_from_artifact(
            content_md=row["content_md"],
            source_lens=lens_name,
            llm_fallback=llm_fallback,
            ticker=ticker,
        )
        if not candidates:
            tally["no_recommendation"] += 1
            continue
        # Take the first candidate per artifact — the lens prompt asks for ONE
        # recommendation, multiples are an LLM artifact.
        cand = candidates[0]
        made_at = _parse_dt(row["generated_at"]) or datetime.now(UTC)
        # Check existence first so we can tally skipped vs new accurately
        existing = _check_existing(db_path, artifact_id)
        pid = record_decision(
            ticker=ticker,
            recommendation_kind=cand.kind,
            recommendation_value=cand.value,
            conviction=cand.conviction,
            source_artifact_id=artifact_id,
            source_lens=cand.source_lens,
            rationale_excerpt=cand.rationale_excerpt,
            made_at=made_at,
            db_path=db_path,
        )
        if pid is None:
            continue
        if existing:
            tally["skipped_existing"] += 1
        else:
            tally["inserted"] += 1
    return tally


def _check_existing(db_path: Path, artifact_id: int) -> bool:
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM decisions WHERE source_artifact_id = ?", (artifact_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()

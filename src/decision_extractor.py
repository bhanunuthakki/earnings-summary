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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

log = logging.getLogger(__name__)


RecommendationKind = Literal["add", "trim", "hold", "sell", "initiate", "avoid"]
UserAction = Literal["followed", "ignored", "partial", "reversed"]
OutcomeLabel = Literal["correct", "wrong", "mixed", "unfalsifiable", "pending"]

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
    source_artifact_id: int
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


def _llm_fallback_extract(
    *, body: str, ticker: str, source_lens: str
) -> list[DecisionCandidate]:
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
        conviction_raw if isinstance(conviction_raw, str) and conviction_raw in {"low", "medium", "high"} else None
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
        path = _resolve(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0)
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


def _resolve(override: Path | str | None) -> Path | None:
    if override is not None:
        return Path(override)
    try:
        from db import DB_PATH

        return Path(DB_PATH)
    except ImportError:
        return None


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
    db_path: Path | str | None = None,
) -> int | None:
    """Idempotent insert. Returns the row id (existing or new) or None on
    DB unavailability. Idempotency is by source_artifact_id via the UNIQUE
    index — second insert on the same artifact returns the existing row."""
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

        cur = conn.execute(
            """
            INSERT INTO decisions(
                ticker, recommendation_kind, recommendation_value, conviction,
                source_artifact_id, source_lens, rationale_excerpt,
                made_at, outcome_label, created_at
            ) VALUES (?,?,?,?,?,?,?,?, 'pending', ?)
            """,
            (
                ticker.upper(),
                recommendation_kind,
                recommendation_value,
                conviction,
                source_artifact_id,
                source_lens,
                rationale_excerpt,
                made_at.isoformat(),
                datetime.now(UTC).isoformat(),
            ),
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
        source_artifact_id=int(row["source_artifact_id"]),
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
) -> dict[str, int]:
    """Walk recent lens artifacts of the named purposes, extract decisions,
    upsert into the decisions table.

    Returns a tally: {inserted, skipped_existing, no_recommendation, db_unavailable}.
    """
    db_path = repo_root / "data" / "portfolio.db"
    tally = {"inserted": 0, "skipped_existing": 0, "no_recommendation": 0, "db_unavailable": 0}
    if not db_path.exists():
        tally["db_unavailable"] = 1
        return tally

    conn = sqlite3.connect(str(db_path), timeout=5.0)
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
    finally:
        conn.close()

    for row in rows:
        artifact_id = int(row["id"])
        ticker = row["ticker"]
        if not ticker:
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

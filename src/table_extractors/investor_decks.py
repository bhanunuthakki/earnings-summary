"""LLM extractor for investor-deck strategic targets.

Investor decks (annual investor days, AGM decks, conference presentations,
capital-markets days) carry the multi-year commitments that quarterly
earnings releases don't: revenue trajectory through 2027, FCF margin
target by 2030, segment KPI ambitions, buyback authorizations, dividend
policy, M&A intent. These slides aren't in any XBRL feed and aren't
covered by the 10-K narrative extractor — they live as PDFs cached under
`ir_documents/<TICKER>/<period>/`.

This module discovers every cached deck for a ticker, parses each to
text via pypdf, and asks Claude to enumerate every forward-looking
target and strategic commitment. The output lands in two tables:

  strategic_targets         — every row (quantitative + qualitative).
  forward_looking_statements — quantitative rows whose target_period can
                                be parsed into a concrete date.

The extractor is opportunistic: a ticker with no decks logs a 'no_data'
result and returns 0 rows — never raises. The unique index on
(ticker, deck_doc_id, target_kind, target_period, substr(excerpt,1,128))
makes re-runs idempotent.

Cost discipline: one LLM call per deck (Sonnet 4.6). A typical deck is
~30-50 pages; the prompt budget is capped at 200 KB of extracted text
so we don't push into the long-context tail of the bill. Budget enforcer
caps spend at $5/month for the 'investor_deck_extraction' purpose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from llm_client import JSON_FENCE_RE, call_llm
from parser import extract_text_from_pdf

log = logging.getLogger(__name__)

EXTRACTOR_ID = "investor_decks_v1"
EXTRACTOR_VERSION = "1"
LLM_PURPOSE = "investor_deck_extraction"

# Cap per-deck input text so a 100-page CMD deck doesn't push into the
# long-context tier. 200KB ≈ 50 dense pages — enough for the strategic
# sections; the boilerplate operating-metric backup is in supplements.
_MAX_INPUT_CHARS = 200_000

# Supported target_kind values. Mirrors the enum documented in migration
# 0053_strategic_targets and used by the unique index. Used to validate
# the LLM's output — unknown kinds get dropped (with a debug log).
_VALID_KINDS: frozenset[str] = frozenset(
    {
        "revenue",
        "fcf_margin",
        "oi_margin",
        "gross_margin",
        "segment_kpi",
        "headcount",
        "m_a_intent",
        "buyback_authorization",
        "dividend_policy",
        "capital_return_pct",
        "capex_intent",
        "strategic_priority",
    }
)

# Quantitative kinds — these get mirrored into forward_looking_statements
# when their target_period can be resolved to a date. Qualitative kinds
# (strategic_priority, m_a_intent, dividend_policy without $ figure) stay
# in strategic_targets only.
_QUANTITATIVE_KINDS: frozenset[str] = frozenset(
    {
        "revenue",
        "fcf_margin",
        "oi_margin",
        "gross_margin",
        "segment_kpi",
        "headcount",
        "buyback_authorization",
        "capital_return_pct",
        "capex_intent",
    }
)

# Deck-shaped filenames. Case-insensitive substring match. Matches the
# repo's `ir_presentation__*.pdf` / `ir_investor_update__*.pdf` /
# `ir_event__*.pdf` cache convention plus the spec's `*deck*` / `*investor*` /
# `*analyst_day*` shapes for files not yet routed through categorize_ir_uploads.
_DECK_FILENAME_TOKENS: tuple[str, ...] = (
    "ir_presentation",
    "ir_investor_update",
    "ir_event",
    "deck",
    "investor",
    "analyst_day",
    "investor_day",
)

# Folder names whose entire contents count as decks. Covers the spec's
# special-folder rule (investor_day/, analyst_day/) for ad-hoc dumps.
_DECK_FOLDER_TOKENS: tuple[str, ...] = ("investor_day", "analyst_day")


_PROMPT = """You are extracting forward-looking targets and strategic commitments from
{ticker}'s investor presentation. The document is an investor day deck,
capital-markets day, AGM presentation, or quarterly investor update —
the slides where management commits to multi-year targets that aren't
in the 10-K.

For EVERY explicit target or commitment, emit a JSON row with:

{{
  "target_kind": "<one of: revenue | fcf_margin | oi_margin | gross_margin |
                  segment_kpi | headcount | m_a_intent |
                  buyback_authorization | dividend_policy |
                  capital_return_pct | capex_intent | strategic_priority>",
  "target_value": <number or null>,
  "target_unit": "<%|USD_M|count|qualitative or ISO currency code like EUR_M>",
  "target_period": "<FY2027 | LT | 2030 | QoQ | next 5 years | ...>",
  "target_currency": "<USD|EUR|DKK|BRL|null>",
  "narrative_excerpt": "<verbatim 60-200 char snippet from the deck>",
  "confidence": <0.0-1.0; default 1.0 if explicit, drop to 0.6 if inferred>
}}

Rules:
  - Return ONLY a JSON array. No commentary. No markdown fence.
  - Aim for completeness — a typical investor day deck has 8-25 rows.
  - target_value is NULL for qualitative kinds (m_a_intent describing
    "selective tuck-ins" without a $ figure; strategic_priority describing
    "expand into emerging markets"; dividend_policy as "progressive growth").
  - For percentage targets, use the % as the number — "FCF margin >30%"
    becomes target_value=30, target_unit="%".
  - For dollar targets, normalize to millions — "$2.5B buyback" becomes
    target_value=2500, target_unit="USD_M", target_currency="USD".
  - For non-USD filers (Novo Nordisk: DKK; MELI: USD; DLO: USD), set the
    target_currency to the disclosed currency and target_unit to
    "<CCY>_M" (e.g. "DKK_M").
  - target_period: copy the period label the deck uses. "FY2027",
    "2030", "long-term", "next 5 years", "QoQ", "by 2028". Don't invent.
  - narrative_excerpt must be verbatim — a phrase actually visible in
    the deck. Don't paraphrase. 60-200 chars.
  - Skip status updates ("we delivered 22% growth last year") — only
    forward-looking commitments belong here.
  - Skip generic boilerplate ("we are committed to shareholder value") —
    those carry no testable target.

INPUT TEXT (deck pages concatenated, may be truncated):
{deck_text}
"""


@dataclass(slots=True)
class _DiscoveredDeck:
    """One deck file plus its resolved documents.id (if any) and period."""

    path: Path
    deck_doc_id: int | None
    period_label: str  # 'YYYY-MM-DD' if known, else file dirname or 'unknown'


def extract_for_ticker(
    ticker: str,
    db: Path,
    *,
    repo_root: Path | None = None,
    ir_root: Path | None = None,
    force_refresh: bool = False,
) -> dict[str, int]:
    """Discover every cached investor deck for ``ticker``, extract targets,
    persist into strategic_targets + forward_looking_statements.

    Args:
        ticker: Equity ticker symbol (case-insensitive).
        db: Path to portfolio.db.
        repo_root: Repo root (used to make stored file_path relative). If
            None, inferred from db.parent.parent.
        ir_root: Override for the ir_documents directory. If None, uses
            ``repo_root / 'ir_documents'``.
        force_refresh: When True, the LLM is called even when an identical
            (ticker, deck_doc_id) tuple already has rows. Default False is
            idempotent — the unique index handles that — so this only
            matters as a way to re-spend the LLM budget on a deck.

    Returns:
        A dict with counts:
          decks_found          — how many deck PDFs were discovered.
          decks_processed      — how many we ran the LLM against.
          rows_inserted        — total strategic_targets rows inserted.
          forward_inserted     — total forward_looking_statements rows inserted.
          llm_failed           — decks the LLM raised on.
          parse_failed         — decks whose LLM output didn't parse.
    """
    ticker_u = ticker.upper()
    repo = repo_root or db.parent.parent
    ir_dir = ir_root or (repo / "ir_documents")

    counts = {
        "decks_found": 0,
        "decks_processed": 0,
        "rows_inserted": 0,
        "forward_inserted": 0,
        "llm_failed": 0,
        "parse_failed": 0,
    }

    decks = list(_discover_decks(ir_dir=ir_dir, ticker=ticker_u, db=db))
    counts["decks_found"] = len(decks)
    if not decks:
        log.info({"event": "no_decks_found", "ticker": ticker_u, "ir_dir": str(ir_dir)})
        return counts

    for deck in decks:
        log.info(
            {
                "event": "extract_deck_start",
                "ticker": ticker_u,
                "deck_path": str(deck.path),
                "deck_doc_id": deck.deck_doc_id,
                "period": deck.period_label,
            }
        )

        # Skip-if-already-done check: if we have rows for this (ticker,
        # deck_doc_id) tuple and force_refresh is False, no LLM spend.
        if (
            not force_refresh
            and deck.deck_doc_id is not None
            and _has_existing_rows(db=db, ticker=ticker_u, deck_doc_id=deck.deck_doc_id)
        ):
            log.info(
                {
                    "event": "deck_already_extracted",
                    "ticker": ticker_u,
                    "deck_doc_id": deck.deck_doc_id,
                }
            )
            continue

        deck_text = _read_deck_text(deck.path)
        if not deck_text:
            log.warning(
                {
                    "event": "deck_text_empty",
                    "ticker": ticker_u,
                    "deck_path": str(deck.path),
                }
            )
            continue

        prompt = _PROMPT.format(ticker=ticker_u, deck_text=deck_text)
        try:
            raw = call_llm(
                prompt,
                purpose=LLM_PURPOSE,
                ticker=ticker_u,
            ).strip()
        except Exception as exc:  # noqa: BLE001 — defensive across provider errors
            log.warning(
                {
                    "event": "deck_llm_failed",
                    "ticker": ticker_u,
                    "deck_path": str(deck.path),
                    "error": str(exc)[:200],
                }
            )
            counts["llm_failed"] += 1
            continue

        rows = _parse_rows(raw)
        if rows is None:
            log.warning(
                {
                    "event": "deck_parse_failed",
                    "ticker": ticker_u,
                    "deck_path": str(deck.path),
                    "raw_head": raw[:200],
                }
            )
            counts["parse_failed"] += 1
            continue

        counts["decks_processed"] += 1
        inserted, fwd_inserted = _persist(
            rows,
            ticker=ticker_u,
            deck_doc_id=deck.deck_doc_id,
            db_path=db,
        )
        counts["rows_inserted"] += inserted
        counts["forward_inserted"] += fwd_inserted
        log.info(
            {
                "event": "deck_extracted",
                "ticker": ticker_u,
                "deck_path": str(deck.path),
                "rows_inserted": inserted,
                "forward_inserted": fwd_inserted,
            }
        )

    return counts


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_decks(*, ir_dir: Path, ticker: str, db: Path) -> Iterable[_DiscoveredDeck]:
    """Walk ir_documents/<TICKER>/ + ir_documents/<TICKER>/*/ for deck-shaped PDFs.

    Yields one _DiscoveredDeck per qualifying file, with deck_doc_id resolved
    against the documents table when the file is registered there.
    """
    base = ir_dir / ticker
    if not base.exists():
        return

    # Build a (sha256 → documents.id) map once so each file lookup is O(1).
    # Falls back to filename matching when sha isn't computable / row missing.
    sha_to_doc_id, path_to_doc_id = _doc_id_lookup_maps(db=db, ticker=ticker)

    for pdf in base.rglob("*.pdf"):
        if not _is_deck_file(pdf):
            continue
        deck_doc_id = _resolve_deck_doc_id(
            pdf, sha_to_doc_id=sha_to_doc_id, path_to_doc_id=path_to_doc_id
        )
        period_label = pdf.parent.name if pdf.parent != base else "unknown"
        yield _DiscoveredDeck(path=pdf, deck_doc_id=deck_doc_id, period_label=period_label)


def _is_deck_file(path: Path) -> bool:
    """Return True when the file looks like an investor deck.

    Match rules (case-insensitive):
      1. Filename contains any of _DECK_FILENAME_TOKENS.
      2. File lives under a folder whose name contains any of
         _DECK_FOLDER_TOKENS (investor_day/, analyst_day/).
    """
    name = path.name.lower()
    for token in _DECK_FILENAME_TOKENS:
        if token in name:
            return True
    # Walk parent folders — folder-shaped match.
    for part in path.parts:
        p_lower = part.lower()
        for token in _DECK_FOLDER_TOKENS:
            if token in p_lower:
                return True
    return False


def _doc_id_lookup_maps(*, db: Path, ticker: str) -> tuple[dict[str, int], dict[str, int]]:
    """Load (sha256 → doc_id) and (relative_path → doc_id) maps for ticker.

    Defensive: returns empty maps when the DB / table is missing. The
    extractor falls back to inserting rows with deck_doc_id=None.
    """
    if not db.exists():
        return {}, {}
    sha_map: dict[str, int] = {}
    path_map: dict[str, int] = {}
    try:
        conn = sqlite3.connect(str(db))
        try:
            present = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
            ).fetchone()
            if present is None:
                return {}, {}
            rows = conn.execute(
                """
                SELECT id, sha256, file_path
                FROM documents
                WHERE ticker = ? AND source_type = 'ir_doc'
                """,
                (ticker,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "doc_lookup_failed",
                "ticker": ticker,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return {}, {}
    for row in rows:
        doc_id = int(row[0])
        sha = str(row[1]) if row[1] is not None else ""
        rel_path = str(row[2]) if row[2] is not None else ""
        if sha:
            sha_map[sha] = doc_id
        if rel_path:
            # Normalize path separators so Windows / POSIX agree.
            path_map[rel_path.replace("\\", "/")] = doc_id
    return sha_map, path_map


def _resolve_deck_doc_id(
    pdf: Path,
    *,
    sha_to_doc_id: dict[str, int],
    path_to_doc_id: dict[str, int],
) -> int | None:
    """Best-effort match a file on disk to a documents.id row.

    Tries sha256 first (most robust — survives moves), falls back to a
    suffix match on file_path (since rows store paths relative to repo root).
    Returns None when no row matches — the extractor still inserts strategic
    targets with deck_doc_id=NULL.
    """
    if sha_to_doc_id:
        try:
            sha = _sha256_of(pdf)
        except OSError:
            sha = None
        if sha and sha in sha_to_doc_id:
            return sha_to_doc_id[sha]

    # Path suffix match: the stored file_path is repo-root-relative; we
    # compare against the trailing segments of pdf.
    pdf_str = str(pdf).replace("\\", "/")
    for rel_path, doc_id in path_to_doc_id.items():
        if pdf_str.endswith(rel_path):
            return doc_id
    return None


def _sha256_of(path: Path, *, chunk: int = 65536) -> str:
    """Stream-hash a file; matches the convention in categorize_ir_uploads."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _read_deck_text(pdf: Path) -> str:
    """Extract text from the deck via the project's pypdf helper. Returns ''
    on any read failure (PDF is encrypted, malformed, etc.) — the caller
    skips empty decks rather than failing the whole ticker."""
    try:
        raw = extract_text_from_pdf(str(pdf))
    except Exception as exc:  # noqa: BLE001 — pypdf raises many types
        log.warning({"event": "pdf_text_failed", "path": str(pdf), "error": str(exc)[:200]})
        return ""
    if not raw:
        return ""
    if len(raw) > _MAX_INPUT_CHARS:
        raw = raw[:_MAX_INPUT_CHARS]
    return raw


# ---------------------------------------------------------------------------
# Parsing + persistence
# ---------------------------------------------------------------------------


def _parse_rows(raw: str) -> list[dict[str, object]] | None:
    """Parse the LLM's JSON output, stripping markdown fences. Returns None
    when the output isn't a JSON array — caller treats as parse_failed."""
    s = raw.strip()
    if s.startswith("```"):
        s = JSON_FENCE_RE.sub("", s).strip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    out: list[dict[str, object]] = []
    for row in cast("list[object]", parsed):
        if isinstance(row, dict):
            out.append(cast("dict[str, object]", row))
    return out


def _has_existing_rows(*, db: Path, ticker: str, deck_doc_id: int) -> bool:
    """Return True when strategic_targets already has rows for (ticker, deck_doc_id)."""
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                """
                SELECT 1 FROM strategic_targets
                WHERE ticker = ? AND deck_doc_id = ?
                LIMIT 1
                """,
                (ticker, deck_doc_id),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


def _persist(
    rows: list[dict[str, object]],
    *,
    ticker: str,
    deck_doc_id: int | None,
    db_path: Path,
) -> tuple[int, int]:
    """Insert rows into strategic_targets + forward_looking_statements.

    Returns (n_strategic_inserted, n_forward_inserted). Both counts respect
    the unique constraints — duplicates silently skip via IntegrityError.
    """
    if not rows or not db_path.exists():
        return 0, 0

    now_iso = datetime.now(UTC).isoformat()
    strategic_inserted = 0
    forward_inserted = 0

    conn = sqlite3.connect(str(db_path))
    has_strategic = _table_present(conn, "strategic_targets")
    has_forward = _table_present(conn, "forward_looking_statements")
    try:
        for row in rows:
            target_kind_raw = row.get("target_kind")
            narrative_raw = row.get("narrative_excerpt")
            target_period_raw = row.get("target_period")
            target_unit_raw = row.get("target_unit")
            if (
                not isinstance(target_kind_raw, str)
                or not isinstance(narrative_raw, str)
                or not isinstance(target_period_raw, str)
                or not isinstance(target_unit_raw, str)
            ):
                continue
            target_kind = target_kind_raw.strip()
            if target_kind not in _VALID_KINDS:
                log.debug({"event": "deck_unknown_target_kind", "kind": target_kind_raw})
                continue
            narrative = narrative_raw.strip()
            if not narrative:
                continue
            target_period = target_period_raw.strip()
            target_unit = target_unit_raw.strip() or "qualitative"

            target_value = _coerce_target_value(row.get("target_value"))
            currency_raw = row.get("target_currency")
            target_currency = (
                str(currency_raw).strip()
                if isinstance(currency_raw, str) and currency_raw.strip()
                else None
            )
            confidence_raw = row.get("confidence")
            confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else 1.0
            confidence = max(0.0, min(1.0, confidence))

            if has_strategic:
                try:
                    conn.execute(
                        """
                        INSERT INTO strategic_targets(
                            ticker, deck_doc_id, target_kind, target_value,
                            target_unit, target_period, target_currency,
                            narrative_excerpt, extracted_at, confidence
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            ticker,
                            deck_doc_id,
                            target_kind,
                            target_value,
                            target_unit,
                            target_period,
                            target_currency,
                            narrative,
                            now_iso,
                            confidence,
                        ),
                    )
                    strategic_inserted += 1
                except sqlite3.IntegrityError:
                    continue

            # Mirror quantitative rows with a resolvable period into
            # forward_looking_statements so downstream prediction/SayDo
            # tooling can grade them later.
            if (
                has_forward
                and deck_doc_id is not None
                and target_kind in _QUANTITATIVE_KINDS
                and target_value is not None
            ):
                resolved_dt = _resolve_target_period_to_date(target_period)
                if resolved_dt is not None:
                    try:
                        conn.execute(
                            """
                            INSERT INTO forward_looking_statements(
                                ticker, source_doc_id, sentence, speaker, tense,
                                quantifier, kpi_name, target_value, target_period,
                                extracted_at, confidence
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                ticker,
                                deck_doc_id,
                                narrative,
                                "management",
                                "target",
                                target_unit,
                                target_kind,
                                float(target_value),
                                resolved_dt.isoformat(),
                                now_iso,
                                confidence,
                            ),
                        )
                        forward_inserted += 1
                    except sqlite3.IntegrityError:
                        # forward_looking_statements has no logical unique
                        # constraint — IntegrityError here would be a schema
                        # mismatch. Log and continue.
                        log.debug(
                            {
                                "event": "forward_looking_insert_failed",
                                "ticker": ticker,
                            }
                        )
        conn.commit()
    finally:
        conn.close()
    return strategic_inserted, forward_inserted


def _coerce_target_value(value: object) -> float | None:
    """Coerce a JSON value to float or None. Defensive — the LLM occasionally
    emits strings like '30%' or '2,500'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").rstrip("%")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


# Date resolution for forward_looking_statements.target_period (DateTime).
# We accept the most common period shapes filers use; anything that doesn't
# parse leaves the row in strategic_targets only.
_PERIOD_FY_RX = re.compile(r"^(?:FY)?\s*(20\d\d)$", re.IGNORECASE)
_PERIOD_YEAR_RX = re.compile(r"^(20\d\d)$")
_PERIOD_QX_YYYY_RX = re.compile(r"^(Q[1-4])\s*[/\s]*(?:FY)?(20\d\d)$", re.IGNORECASE)
_PERIOD_BY_YEAR_RX = re.compile(r"^(?:by|through)\s+(20\d\d)$", re.IGNORECASE)


def _resolve_target_period_to_date(period: str) -> datetime | None:
    """Convert a target_period string to a concrete UTC datetime.

    Returns None for unparseable periods ('LT', 'long-term', 'next 5 years',
    'QoQ'), which is fine — those rows stay in strategic_targets only.
    """
    s = period.strip()
    if not s:
        return None

    m = _PERIOD_FY_RX.match(s) or _PERIOD_YEAR_RX.match(s) or _PERIOD_BY_YEAR_RX.match(s)
    if m:
        try:
            year = int(m.group(1))
        except ValueError:
            return None
        return datetime(year, 12, 31, tzinfo=UTC)

    qm = _PERIOD_QX_YYYY_RX.match(s)
    if qm:
        quarter = qm.group(1).upper()
        try:
            year = int(qm.group(2))
        except ValueError:
            return None
        month_day = {"Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "Q4": (12, 31)}.get(quarter)
        if month_day is None:
            return None
        return datetime(year, month_day[0], month_day[1], tzinfo=UTC)

    return None


def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None

"""LLM extractor for customer-concentration disclosures.

Customer concentration is narrative-only in US-GAAP 10-Ks — it's not a
distinct XBRL table. The disclosure typically appears in:
  - Item 1 (Business) narrative — 'one customer accounted for X% of revenue'
  - Summary of Significant Accounting Policies — 'Concentrations of Credit Risk'
  - Footnote text — Commitments / Customer Risk / Major Customers

This extractor concatenates the relevant FMP-section text into one
Haiku-sized prompt and parses the returned JSON into
`customer_concentrations` rows. Customer entities are resolved against
the existing entity graph; anonymized labels ('Customer A', 'a major
hyperscaler') get per-ticker-scoped entity rows.

Cost discipline: one Haiku call per ticker per filing year. Cap input
at 80KB.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from llm_client import JSON_FENCE_RE, call_llm
from table_extractors.base import ExtractionOutcome

# Aliases for the entity_store callables we lazy-import per call.
_ResolveFn = Callable[..., int | None]
_UpsertFn = Callable[..., int | None]
_ProposeFn = Callable[..., tuple[int | None, str]]

log = logging.getLogger(__name__)

EXTRACTOR_ID = "customer_concentration_v1"
EXTRACTOR_VERSION = "1"
TABLE_KIND = "customer_concentration"

_MAX_INPUT_CHARS = 80_000

# Sections in the FMP JSON most likely to contain customer-concentration
# disclosures. We concatenate any matching sections (substring, case-
# insensitive) and pass the combined text to the LLM.
_RELEVANT_SECTION_KEYWORDS: tuple[str, ...] = (
    "significant accounting policies",
    "revenue",
    "commitments and contingencies",
    "concentration",
    "credit risk",
    "customer",
    "major customer",
)

# Anonymized customer-label detector. Matches 'Customer A', 'Customer B',
# 'Customer C', and common variants like 'a major hyperscaler',
# 'a large cloud provider', etc.
_ANONYMIZED_RX = re.compile(
    r"^(customer\s+[A-Z]\b|a\s+(major|large|significant|single)\s+\w+|"
    r"one\s+customer|a\s+single\s+customer|undisclosed\s+customer)",
    re.IGNORECASE,
)


_PROMPT = """You are extracting customer-concentration disclosures from {ticker}'s
fiscal year {fiscal_year} 10-K. Many filers disclose that a specific
customer (or a small number of customers) accounted for a material
percentage of revenue. Your job is to surface every such disclosure
in this filing.

For each disclosure, emit a JSON row with:

{{
  "customer_label": "<verbatim name or label>",
  "pct_of_revenue": <number between 0 and 1, e.g. 0.12 for 12%>,
  "revenue_amount": <USD millions when given, else null>,
  "anonymized": <true if the label is 'Customer A' / 'a major hyperscaler' / etc., else false>,
  "source_excerpt": "<verbatim 80-200 char snippet that supports the row>"
}}

Rules:
  - Return ONLY a JSON array of rows. No commentary. No markdown fence.
  - 0 to ~8 rows is typical. Return [] if the filing names no concentration
    or only says 'no single customer accounted for >10% of revenue'.
  - Skip generic statements with no specific percentage.
  - When the disclosure says 'X% of revenue', use that as pct_of_revenue.
    If it says 'less than 10%' without a precise number, SKIP the row
    (we can't store ranges).
  - source_excerpt must be a real verbatim phrase from the input — copy
    it; don't paraphrase.
  - Distinguish CUSTOMER concentration from SUPPLIER concentration; only
    emit rows about revenue concentration (the customer paying us), not
    input-supplier dependence.

INPUT TEXT (concatenated relevant sections, may be truncated):
{filing_text}
"""


def extract(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None,
    sec_text: object | None,
    db_path: Path,
    repo_root: Path,
    filing_doc_id: int | None = None,
    as_of_date: object | None = None,
) -> ExtractionOutcome:
    """Call the LLM, parse the rows, persist into `customer_concentrations`."""
    del repo_root, as_of_date  # unused

    if fiscal_year is None:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=None,
            status="no_data",
            notes="fiscal_year required",
        )

    # Prefer FMP-derived text (it's cleaner than scraped 10-K HTML); fall
    # back to sec_text.text when FMP isn't present.
    filing_text = _filing_text_from_fmp(fmp_payload)
    if not filing_text and sec_text is not None:
        # sec_text may be a FilingTextResult dataclass; extract .text
        # defensively.
        text_attr = getattr(sec_text, "text", None)
        if isinstance(text_attr, str):
            filing_text = text_attr[:_MAX_INPUT_CHARS]
    if not filing_text:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="no_data",
            notes="no filing text available",
        )

    prompt = _PROMPT.format(ticker=ticker, fiscal_year=fiscal_year, filing_text=filing_text)

    try:
        raw = call_llm(
            prompt,
            purpose="customer_concentration_extraction",
            ticker=ticker,
            model="claude-haiku-4-5-20251001",
        ).strip()
    except Exception as exc:  # noqa: BLE001 — defensive across provider errors
        log.warning(
            {"event": "customer_concentration_llm_failed", "ticker": ticker, "error": str(exc)}
        )
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="llm_failed",
            notes=str(exc)[:200],
        )

    rows = _parse_rows(raw)
    if rows is None:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="parse_failed",
            notes=raw[:200],
        )

    inserted, logged, proposals = _persist(
        rows,
        ticker=ticker,
        fiscal_year=fiscal_year,
        filing_doc_id=filing_doc_id,
        db_path=db_path,
    )
    return ExtractionOutcome(
        table_kind=TABLE_KIND,
        ticker=ticker,
        fiscal_year=fiscal_year,
        n_rows_proposed=len(rows),
        n_rows_inserted=inserted,
        n_extractions_logged=logged,
        n_mapping_proposals=proposals,
        status="ok",
    )


def _filing_text_from_fmp(payload: dict[str, object] | None) -> str:
    """Concatenate relevant sections into a single text blob, capped."""
    if not payload:
        return ""
    chunks: list[str] = []
    total = 0
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if not any(kw in key.lower() for kw in _RELEVANT_SECTION_KEYWORDS):
            continue
        chunk = f"## {key}\n{_serialize_section(value)}\n"
        if total + len(chunk) > _MAX_INPUT_CHARS:
            chunks.append(chunk[: _MAX_INPUT_CHARS - total])
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n".join(chunks)


def _serialize_section(value: object) -> str:
    """FMP sections are list-of-dicts; flatten into key: value lines."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        lines: list[str] = []
        items = cast("list[object]", value)
        for entry in items:
            if isinstance(entry, dict):
                for k, v in cast("dict[str, object]", entry).items():
                    if isinstance(v, list):
                        nested = ", ".join(str(x) for x in cast("list[object]", v))
                        lines.append(f"{k}: {nested}")
                    else:
                        lines.append(f"{k}: {v}")
            else:
                lines.append(str(entry))
        return "\n".join(lines)
    return str(value)


def _parse_rows(raw: str) -> list[dict[str, object]] | None:
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


def _persist(
    rows: list[dict[str, object]],
    *,
    ticker: str,
    fiscal_year: int,
    filing_doc_id: int | None,
    db_path: Path,
) -> tuple[int, int, int]:
    """Insert into customer_concentrations + entity resolution +
    extractions log. Returns (n_inserted, n_logged, n_mapping_proposals).

    Resolution happens BEFORE the writer connection opens so the
    entity_store's own SQLite connections don't race with ours (SQLite
    locks the file for writes, not just rows).
    """
    if not rows or not db_path.exists():
        return 0, 0, 0

    # Lazy-import entity_store to avoid the cost when no rows exist.
    from entity_store import propose_mapping, resolve_entity, upsert_entity

    fiscal_period = str(fiscal_year)
    now_iso = datetime.now(UTC).isoformat()

    # ---- Phase 1: normalize + resolve. No writer connection yet. ----
    prepared: list[dict[str, object]] = []
    proposals = 0
    for row in rows:
        label_raw = row.get("customer_label")
        pct_raw = row.get("pct_of_revenue")
        if not isinstance(label_raw, str) or not label_raw.strip():
            continue
        if not isinstance(pct_raw, (int, float)):
            continue
        label = label_raw.strip()
        pct = float(pct_raw)
        if not (0.0 <= pct <= 1.0):
            # Some models emit pct as 12 (meaning 12) — normalize.
            if 1.0 < pct <= 100.0:
                pct = pct / 100.0
            else:
                continue
        revenue_raw = row.get("revenue_amount")
        revenue_amount = float(revenue_raw) if isinstance(revenue_raw, (int, float)) else None
        anonymized = bool(row.get("anonymized"))
        if not anonymized:
            anonymized = bool(_ANONYMIZED_RX.match(label))
        source_excerpt_raw = row.get("source_excerpt") or ""
        source_excerpt = (source_excerpt_raw if isinstance(source_excerpt_raw, str) else "")[:1024]

        customer_entity_id = _resolve_or_create_customer(
            label=label,
            anonymized=anonymized,
            ticker=ticker,
            source_doc_id=filing_doc_id,
            source_excerpt=source_excerpt,
            db_path=db_path,
            resolve_fn=resolve_entity,
            upsert_fn=upsert_entity,
            propose_fn=propose_mapping,
        )
        if customer_entity_id is None and not anonymized:
            proposals += 1  # named-but-unresolved → proposal was emitted
        prepared.append(
            {
                "label": label,
                "pct": pct,
                "revenue_amount": revenue_amount,
                "anonymized": anonymized,
                "source_excerpt": source_excerpt,
                "customer_entity_id": customer_entity_id,
            }
        )

    # ---- Phase 2: writer connection. No external code paths take a
    # connection while we hold this one. ----
    inserted = 0
    logged = 0
    conn = sqlite3.connect(str(db_path))
    has_extractions = _table_present(conn, "customer_concentrations")
    has_ext_log = _table_present(conn, "extractions")
    try:
        for p in prepared:
            label = cast("str", p["label"])
            pct = cast("float", p["pct"])
            revenue_amount = cast("float | None", p["revenue_amount"])
            anonymized = cast("bool", p["anonymized"])
            source_excerpt = cast("str", p["source_excerpt"])
            customer_entity_id = cast("int | None", p["customer_entity_id"])

            if not has_extractions:
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO customer_concentrations(
                        ticker, fiscal_period, fiscal_period_type,
                        customer_label, customer_entity_id, pct_of_revenue,
                        revenue_amount, revenue_currency, source_doc_id,
                        source_excerpt, extracted_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ticker,
                        fiscal_period,
                        "FY",
                        label,
                        customer_entity_id,
                        pct,
                        revenue_amount,
                        "USD",
                        filing_doc_id,
                        source_excerpt,
                        now_iso,
                    ),
                )
                inserted += 1
                if has_ext_log and filing_doc_id is not None:
                    conn.execute(
                        """
                        INSERT INTO extractions(
                            source_doc_id, extraction_kind, extractor_id,
                            extractor_version, payload_json, confidence,
                            extracted_at, links_to_json
                        ) VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(filing_doc_id),
                            "customer_concentration",
                            EXTRACTOR_ID,
                            EXTRACTOR_VERSION,
                            json.dumps(
                                {
                                    "ticker": ticker,
                                    "fiscal_period": fiscal_period,
                                    "customer_label": label,
                                    "anonymized": anonymized,
                                    "pct_of_revenue": pct,
                                    "revenue_amount": revenue_amount,
                                    "source_excerpt": source_excerpt,
                                }
                            ),
                            1.0,
                            now_iso,
                            json.dumps(
                                {
                                    "table": "customer_concentrations",
                                    "ticker": ticker,
                                    "fiscal_period": fiscal_period,
                                    "customer_label": label,
                                }
                            ),
                        ),
                    )
                    logged += 1
            except sqlite3.IntegrityError:
                # Unique on (ticker, fiscal_period, customer_label).
                continue
        conn.commit()
    finally:
        conn.close()
    return inserted, logged, proposals


def _resolve_or_create_customer(
    *,
    label: str,
    anonymized: bool,
    ticker: str,
    source_doc_id: int | None,
    source_excerpt: str,
    db_path: Path,
    resolve_fn: _ResolveFn,
    upsert_fn: _UpsertFn,
    propose_fn: _ProposeFn,
) -> int | None:
    """Resolve a customer label to entities.id.

    Anonymized labels get a ticker-scoped canonical name like
    'GOOG Customer A' so they don't collide across issuers. Named
    customers try a global resolve first; on miss, emit a mapping
    proposal at mid confidence so a human can review.
    """
    if anonymized:
        canonical = f"{ticker} {label}"
        eid = resolve_fn(canonical, kind="customer", db_path=db_path)
        if eid is not None:
            return eid
        return upsert_fn(
            kind="customer",
            canonical_name=canonical,
            display_name=label,
            meta={"anonymized": True, "issuer_ticker": ticker},
            db_path=db_path,
        )

    # Named customer: try a global resolve.
    resolved = resolve_fn(label, kind="customer", db_path=db_path)
    if resolved is not None:
        return resolved
    # Try kind='company' — sometimes the customer is itself a tracked
    # company and was registered as kind='company' first.
    resolved_co = resolve_fn(label, kind="company", db_path=db_path)
    if resolved_co is not None:
        return resolved_co
    # No match → propose. Mid confidence (0.6) so it queues for review.
    propose_fn(
        kind="new_entity",
        payload={
            "kind": "customer",
            "canonical_name": label,
            "display_name": label,
        },
        confidence=0.6,
        proposed_by=EXTRACTOR_ID,
        ticker=ticker,
        source_doc_id=source_doc_id,
        source_excerpt=source_excerpt,
        db_path=db_path,
    )
    return None


def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None

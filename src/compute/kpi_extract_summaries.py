"""Stages 2-3 of the KPI cascade — extract custom tier_1_kpis from LLM summaries.

Stage 1 (`fmp_derived_kpis`) handled universal financial KPIs deterministically
from FMP raw data. Stages 2/3 reach into the LLM-generated summaries already
on disk to fill in custom per-ticker metrics (GMV, ARPAC, NIM, etc.) that
aren't derivable from raw line items.

Two SourceType variants share the same pipeline shape, just different filename
suffixes and slightly different doc_type labels for provenance:

  - "earnings"  (Stage 2): `_summary.txt` + `_investor_update_summary.txt`
                           — per-quarter LLM summary of the call/investor letter
  - "ir"        (Stage 3): `_press_release_summary.txt` + `_presentation_brief.txt`
                           — IR-pipeline structured briefs

Pipeline per (ticker, quarter, source-doc):
  1. Locate matching files in `.tmp/`.
  2. Auto-register each as a `documents` row (`source_type=llm_extracted`,
     doc_type per source) so kpi_facts can FK to it.
  3. From the holdings JSON tier_1_kpis, list KPI names NOT yet present in
     kpi_facts for this (ticker, period). Skip if all are already extracted.
  4. Single Haiku call returning {kpi_name: {value, unit, confidence}}.
  5. Persist via the existing KpiExtractionManifest pipeline.

Idempotent on (ticker, period_end, kpi_definition_id): re-runs skip rows whose
missing-KPI list is empty. Force re-extraction with `--refresh`.

Per-run state lands in `data/kpi_extraction_log.json` keyed by ticker → stage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from compute.kpi_resolver import normalize_kpi_name
from llm_client import FAST_CLASSIFIER_MODEL, JSON_FENCE_RE, _call_claude
from models.documents import SourceType, tier_for_source_type
from models.facts import FiscalPeriodType, Unit
from models.unit_convert import same_family
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from pipeline.restatement_detector import _table_has_column
from pipeline.run_accounting import start_run

# Named `logger` (not `log`) to avoid colliding with the `TickerExtractionLog`
# locals named `log` throughout this module.
logger = logging.getLogger("kpi_extract_summaries")

# Per-source filename matchers + the documents.doc_type label written for each.
# Earnings: per-quarter call summary (canonical) + the MELI/NU investor-update variant.
# IR: press-release LLM summary + presentation-deck brief from the IR pipeline.
_EARNINGS_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)
_IR_PRESS_RELEASE_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_press_release_summary\.txt$"
)
_IR_PRESENTATION_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_presentation_brief\.txt$"
)


@dataclass(frozen=True)
class _SourceSpec:
    """One LLM-summary kind: filename pattern + the doc_type used for provenance."""

    name: str  # short label for telemetry / log keys
    doc_type: str  # what we write into documents.doc_type
    pattern: re.Pattern[str]


_EARNINGS_SOURCE = _SourceSpec("earnings_summary", "llm_summary", _EARNINGS_SUMMARY_RX)
_IR_PRESS_RELEASE_SOURCE = _SourceSpec(
    "ir_press_release_summary", "ir_press_release_synthesized", _IR_PRESS_RELEASE_RX
)
_IR_PRESENTATION_SOURCE = _SourceSpec(
    "ir_presentation_brief", "ir_presentation_synthesized", _IR_PRESENTATION_RX
)

_SOURCE_GROUPS: dict[str, tuple[_SourceSpec, ...]] = {
    "earnings": (_EARNINGS_SOURCE,),
    "ir": (_IR_PRESS_RELEASE_SOURCE, _IR_PRESENTATION_SOURCE),
}

# How filename Q + calendar year map to a period_end. Most tickers use the
# calendar fiscal-year mapping; the per-ticker override map handles companies
# whose fiscal year ends on a non-December month (RBRK and VEEV both use
# January FYE). NVO publishes H1 / 9M instead of Q2 / Q3, but its period
# end-of-month dates still align to calendar quarters, so no override needed.
_QUARTER_PERIOD_END: dict[int, tuple[int, int]] = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}

# (month, day) per fiscal quarter for tickers with non-calendar fiscal years.
# When the month is in Jan/Feb, period_end falls into calendar `year + 1`
# (e.g. RBRK_Q4_2026 = FY26 Q4 ending 2027-01-31).
_TICKER_QUARTER_PERIOD_END: dict[str, dict[int, tuple[int, int]]] = {
    "RBRK": {1: (4, 30), 2: (7, 31), 3: (10, 31), 4: (1, 31)},
    "VEEV": {1: (4, 30), 2: (7, 31), 3: (10, 31), 4: (1, 31)},
}


@dataclass
class TickerExtractionLog:
    """Per-ticker run record persisted to data/kpi_extraction_log.json."""

    ticker: str
    stage: str = "stage_2_summaries"
    started_at: str = ""
    ended_at: str = ""
    elapsed_ms: int = 0
    quarters_attempted: list[str] = field(default_factory=list)
    quarters_extracted: list[str] = field(default_factory=list)
    quarters_skipped_no_missing: list[str] = field(default_factory=list)
    kpis_inserted_total: int = 0
    error: str | None = None


_GROUP_TO_STAGE: dict[str, str] = {
    "earnings": "stage_2_summaries",
    "ir": "stage_3_ir_briefs",
}


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    conn: sqlite3.Connection,
    refresh: bool = False,
    source_group: str = "earnings",
) -> TickerExtractionLog:
    ticker = ticker.upper()
    if source_group not in _SOURCE_GROUPS:
        raise ValueError(
            f"unknown source_group {source_group!r}; expected one of {list(_SOURCE_GROUPS)}"
        )
    log = TickerExtractionLog(ticker=ticker, stage=_GROUP_TO_STAGE[source_group])
    log.started_at = _now_iso_z()
    t0 = time.perf_counter()

    sources = _list_sources(repo_root, ticker, _SOURCE_GROUPS[source_group])
    holdings = _read_holdings(repo_root, ticker)
    if holdings is None:
        log.error = f"no holdings JSON for {ticker}"
        return _close_log(log, t0)
    tier_1_names = _tier_1_names(holdings)
    if not tier_1_names:
        log.error = "holdings JSON has no tier_1_kpis"
        return _close_log(log, t0)
    # Veto a break-rule's *comparison* unit (a YoY-growth percent on a dollar
    # LEVEL) using the metric's recorded fact unit before it becomes canonical.
    fact_units = _fact_units_by_name(conn, ticker, tier_1_names)
    canonical_units = _canonical_units_from_holdings(holdings, tier_1_names, fact_units=fact_units)

    for quarter, year, source_path, spec in sources:
        period_end = _period_end(ticker, quarter, year)
        period_label = f"Q{quarter} {year} [{spec.name}]"
        log.quarters_attempted.append(period_label)

        already_extracted = _already_extracted_kpis(
            conn, ticker, period_end, tier_1_names
        )
        missing = [n for n in tier_1_names if n not in already_extracted]
        if not missing and not refresh:
            log.quarters_skipped_no_missing.append(period_label)
            continue

        try:
            doc_id = _ensure_summary_document_row(
                conn, ticker, period_end, source_path, spec.doc_type
            )
            text = source_path.read_text(encoding="utf-8")
            extracted = _llm_extract(
                ticker, period_label, missing if not refresh else tier_1_names, text
            )
            if not extracted:
                continue
            manifest = _build_manifest(
                ticker,
                period_end,
                FiscalPeriodType(f"Q{quarter}"),
                doc_id,
                extracted,
                canonical_units,
            )
            run_id = start_run(
                conn,
                directive=f"extract_kpis_from_{source_group}",
                ticker_scope=[ticker],
            )
            result = persist_manifest(conn, run_id=run_id, manifest=manifest)
            log.kpis_inserted_total += result.inserted
            log.quarters_extracted.append(period_label)
        except Exception as e:
            log.error = f"{period_label}: {type(e).__name__}: {e}"
            break

    return _close_log(log, t0)


def _list_sources(
    repo_root: Path, ticker: str, specs: tuple[_SourceSpec, ...]
) -> list[tuple[int, int, Path, _SourceSpec]]:
    """Walk `.tmp/`, return (quarter, year, path, spec) for every match. Oldest first."""
    tmp = repo_root / ".tmp"
    if not tmp.exists():
        return []
    out: list[tuple[int, int, Path, _SourceSpec]] = []
    for p in tmp.iterdir():
        if not p.is_file():
            continue
        for spec in specs:
            m = spec.pattern.match(p.name)
            if not m or m.group("ticker") != ticker:
                continue
            out.append((int(m.group("q")), int(m.group("y")), p, spec))
            break
    out.sort(key=lambda x: (x[1], x[0], x[3].name))
    return out


def _read_holdings(repo_root: Path, ticker: str) -> dict[str, object] | None:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _tier_1_names(holdings: dict[str, object]) -> list[str]:
    raw = holdings.get("tier_1_kpis") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for k in raw:
        if isinstance(k, dict):
            name = str(k.get("name", "")).strip()
            if name:
                out.append(name)
    return out


def _fact_units_by_name(conn: sqlite3.Connection, ticker: str, names: list[str]) -> dict[str, Unit]:
    """Dominant recorded ``kpi_facts`` unit per metric, for names that have facts.

    The recorded fact unit is the metric's true reported-value unit — used to
    veto a break-rule's *comparison* unit (a YoY-growth ``percent`` declared for
    a dollar LEVEL) from being adopted as the metric's canonical unit. Names with
    no facts, or a stored unit outside the :class:`Unit` enum, are omitted.
    Returns the modal unit per name (ties broken by the latest period).
    """
    out: dict[str, Unit] = {}
    for name in names:
        row = conn.execute(
            "SELECT f.unit FROM kpi_facts f "
            "JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
            "WHERE d.ticker = ? AND d.name = ? AND f.unit IS NOT NULL "
            "GROUP BY f.unit ORDER BY COUNT(*) DESC, MAX(f.period_end) DESC LIMIT 1",
            (ticker.upper(), name),
        ).fetchone()
        if row is None or row[0] is None:
            continue
        try:
            out[name] = Unit(str(row[0]))
        except ValueError:
            continue
    return out


def _canonical_units_from_holdings(
    holdings: dict[str, object],
    kpi_names: list[str],
    *,
    fact_units: dict[str, Unit] | None = None,
) -> dict[str, Unit]:
    """Map each extraction KPI name to its break-rule's declared Unit.

    The break-rules (`break_rules` + `business_model_rules`) declare the unit each
    threshold is expressed in — the authoritative unit the metric should be stored
    in, which `persist_manifest` reconciles the LLM's per-call unit to. They're
    keyed by their own `kpi_name`, which may spell the metric differently from the
    tier_1 label the extractor pulls ("Net New Subscription ARR (USD millions)" vs
    "Net new subscription ARR ($)"); both normalize to the same key, so we match on
    the parenthetical-insensitive normalized name. Only real `Unit` values are
    kept — a rule unit outside the enum (e.g. a derived "percent_decel" transform)
    has no dimensional meaning here and is skipped.

    A rule's unit equals the metric's reported-value unit ONLY for a direct
    level/rate threshold — NOT for a YoY-growth or deceleration rule, whose unit
    is ``percent`` while the metric itself is a dollar LEVEL (the AWS RPO shape).
    When ``fact_units`` supplies a metric's recorded value unit and the rule unit
    is in a DIFFERENT dimensional family, the rule unit is a *comparison* unit and
    is dropped — the metric keeps its real value unit. A same-family rule unit is
    still adopted (the ratio→percent / dollar→millions normalization #320 added).
    Without a fact unit (first extraction) the rule unit is used as before.
    """
    fact_units = fact_units or {}
    rule_unit_by_norm: dict[str, Unit] = {}
    for key in ("break_rules", "business_model_rules"):
        raw = holdings.get(key)
        if not isinstance(raw, list):
            continue
        for entry in cast("list[object]", raw):
            if not isinstance(entry, dict):
                continue
            rule = cast("dict[str, object]", entry)
            rule_name = str(rule.get("kpi_name", "")).strip()
            rule_unit = str(rule.get("unit", "")).strip()
            if not rule_name or not rule_unit:
                continue
            try:
                unit = Unit(rule_unit)
            except ValueError:
                continue
            # First rule wins per metric; break_rules precede business_model_rules,
            # matching the evaluator's universal-then-business-model ordering.
            rule_unit_by_norm.setdefault(normalize_kpi_name(rule_name), unit)
    out: dict[str, Unit] = {}
    for name in kpi_names:
        unit = rule_unit_by_norm.get(normalize_kpi_name(name))
        if unit is None:
            continue
        fact_unit = fact_units.get(name)
        if fact_unit is not None and not same_family(unit, fact_unit):
            # The rule's threshold is a *change* of the metric (e.g. YoY growth in
            # percent), not its level (dollars). Don't let it override the real
            # value unit; the LLM's extracted unit is used unchanged downstream.
            logger.info(
                {
                    "event": "canonical_unit_dropped_cross_family",
                    "kpi_name": name,
                    "rule_unit": unit.value,
                    "fact_unit": fact_unit.value,
                }
            )
            continue
        out[name] = unit
    return out


def _period_end(ticker: str, quarter: int, year: int) -> datetime:
    """Map a (filename year, fiscal quarter) pair to the actual period_end date.

    Most tickers report on the calendar fiscal year (Q4 FY{year} ends
    {year}-12-31). Tickers in `_TICKER_QUARTER_PERIOD_END` use a non-calendar
    fiscal year — for those, the override determines (month, day) and Q4 of a
    January-FYE company rolls into calendar `year + 1`.
    """
    overrides = _TICKER_QUARTER_PERIOD_END.get(ticker.upper())
    if overrides and quarter in overrides:
        month, day = overrides[quarter]
        period_year = year + 1 if month <= 2 else year
        return datetime(period_year, month, day)
    month, day = _QUARTER_PERIOD_END[quarter]
    return datetime(year, month, day)


def _already_extracted_kpis(
    conn: sqlite3.Connection, ticker: str, period_end: datetime, names: list[str]
) -> set[str]:
    if not names:
        return set()
    placeholders = ",".join("?" * len(names))
    cur = conn.execute(
        f"""
        SELECT DISTINCT kd.name
        FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        WHERE kf.ticker = ? AND kf.period_end = ?
          AND kd.name IN ({placeholders})
        """,
        (ticker, period_end, *names),
    )
    return {str(r["name"]) for r in cur.fetchall()}


def _ensure_summary_document_row(
    conn: sqlite3.Connection,
    ticker: str,
    period_end: datetime,
    path: Path,
    doc_type: str,
) -> int:
    """Insert documents row for the summary file if not already present, return id."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    existing = conn.execute(
        "SELECT id FROM documents WHERE sha256 = ?", (sha,)
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    common_args = (
        ticker,
        SourceType.LLM_EXTRACTED.value,
        doc_type,
        period_end,
        str(path).replace("\\", "/"),
        sha,
        datetime.now(timezone.utc),
        "ok",
        len(raw),
    )
    if _table_has_column(conn, "documents", "source_quality_tier"):
        tier = tier_for_source_type(SourceType.LLM_EXTRACTED).value
        cur = conn.execute(
            """
            INSERT INTO documents
              (ticker, source_type, doc_type, period_end, file_path, sha256,
               fetched_at, fetch_status, raw_bytes_size, source_quality_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*common_args, tier),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO documents
              (ticker, source_type, doc_type, period_end, file_path, sha256,
               fetched_at, fetch_status, raw_bytes_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            common_args,
        )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _llm_extract(
    ticker: str, period_label: str, kpi_names: list[str], summary_text: str
) -> dict[str, dict[str, object]]:
    """Single Haiku call. Returns {kpi_name: {value, unit, confidence}}.

    Prompt is generic across all three source shapes (earnings call summary /
    press release / presentation brief). Each has its own structure, but the
    extraction contract — pull the as-reported current-quarter value of each
    named KPI — is the same.
    """
    if not kpi_names:
        return {}
    names_block = "\n".join(f"- {n}" for n in kpi_names)
    prompt = f"""You are extracting structured KPI values from a quarterly research document for {ticker} ({period_label}).

The document is one of: an LLM-summarised earnings call (sections: Financial Highlights table, Operational Highlights, Management Outlook, Q&A), an LLM-summarised IR press release (Headline Results table, Key Business Metrics, Guidance, Capital Allocation), or an LLM-distilled presentation brief (Management Narrative, Highlighted Metrics, Strategic Initiatives, Forward-Looking Slides). Whichever it is, current-quarter actuals live in the headline table / metrics section; *guidance* for the NEXT quarter or full year is reported separately and is NOT what we want here.

For EACH of the KPI names below, find the value reported FOR THIS QUARTER (not guidance for next quarter). Return a JSON object keyed by the EXACT name I gave you. Values:
  - "value": numeric only, no unit symbols (e.g. 12.5 not "12.5%").
  - "unit": EXACTLY ONE of these tokens — "percent", "actual", "count", "ratio", "bps":
      * "percent"  — rates, margins, growth, retention (e.g. NRR 120% → value 120; NIM 17.8% → 17.8). Drop the % sign; do NOT divide by 100.
      * "actual"   — ALL monetary amounts. Report the FULL figure in the issuer's base currency units, NEVER pre-scaled. "$1.2 billion" → value 1200000000; "$115M net new ARR" → 115000000; ARPAC "$11.20" → 11.20. Never use a currency code or a scaled token as the unit — always expand to the whole number with unit "actual".
      * "count"    — headcount / customers / subscribers / accounts, as the full integer (e.g. "12.5 million customers" → 12500000).
      * "ratio"    — unitless multiples reported with an "x" (e.g. coverage 1.8x → 1.8).
      * "bps"      — only when the document states the metric in basis points.
    Use only those five tokens; if unsure between "actual" and a scaled form, always pick "actual" with the full figure.
  - "confidence": float 0.0–1.0; lower if you had to estimate from context

If a KPI is not disclosed in the document, OMIT IT from the response. Do not guess.

KPI names to extract:
{names_block}

Document text:
\"\"\"
{summary_text}
\"\"\"

Return ONLY the JSON object — no markdown fence, no commentary."""

    raw = _call_claude(prompt, model=FAST_CLASSIFIER_MODEL).strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    # Haiku occasionally appends commentary after the JSON; raw_decode peels
    # off the first top-level value and ignores the rest.
    start = raw.find("{")
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(raw[start:])
    if not isinstance(parsed, dict):
        return {}
    return {str(k): v for k, v in parsed.items() if isinstance(v, dict) and "value" in v}


def parse_decimal_value(raw: object) -> Decimal | None:
    """Parse an LLM-returned KPI value to Decimal, or None if unparseable.

    Haiku occasionally returns a non-numeric string ("N/A", "~17%", "n.m.") for a
    KPI it can't find a clean figure for. ``Decimal(str(raw))`` then raises
    ``decimal.InvalidOperation`` (an ArithmeticError, NOT a ValueError), so
    catching only (TypeError, ValueError) let one bad value abort the WHOLE
    ticker's extraction. Returning None lets the caller skip just that KPI.
    """
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _build_manifest(
    ticker: str,
    period_end: datetime,
    fpt: FiscalPeriodType,
    doc_id: int,
    extracted: dict[str, dict[str, object]],
    canonical_units: dict[str, Unit] | None = None,
) -> KpiExtractionManifest:
    values: list[KpiValue] = []
    for name, payload in extracted.items():
        v = parse_decimal_value(payload.get("value"))
        if v is None:
            continue
        unit_raw = str(payload.get("unit") or "actual")
        try:
            unit = Unit(unit_raw)
        except ValueError:
            unit = Unit.ACTUAL
        conf_raw = payload.get("confidence", 0.85)
        try:
            confidence = max(0.0, min(1.0, float(conf_raw)))
        except (TypeError, ValueError):
            confidence = 0.85
        values.append(KpiValue(name=name, value=v, unit=unit, confidence=confidence))

    return KpiExtractionManifest(
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fpt,
        source_doc_id=doc_id,
        primary_source=SourceType.LLM_EXTRACTED,
        model_name=FAST_CLASSIFIER_MODEL,
        canonical_units=canonical_units or {},
        values=values,
    )


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _close_log(log: TickerExtractionLog, t0: float) -> TickerExtractionLog:
    log.ended_at = _now_iso_z()
    log.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return log


def write_log(repo_root: Path, results: list[TickerExtractionLog]) -> Path:
    """Append/overwrite ticker entries in `data/kpi_extraction_log.json`."""
    log_path = repo_root / "data" / "kpi_extraction_log.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        existing = json.loads(log_path.read_text(encoding="utf-8"))
    else:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    for r in results:
        existing.setdefault(r.ticker, {})[r.stage] = asdict(r)
    log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return log_path

"""Deterministic per-fact confidence scoring (fund-grade build S2, PR1).

Every ``financial_facts`` / ``kpi_facts`` row carries a ``confidence`` column
that historically defaulted to 1.0 and was never scored — a 100% badge on an
S-1 extraction and on an SEC XBRL fact alike, which is worse than no badge.
This module is the single source of truth for what the number MEANS:

    confidence = clamp( tier_base + method_delta − issue_penalties ) × self_report

* **tier_base** — the document's ``source_quality_tier`` (`TIER_BASE`).
  An SEC filing is the issuer's own audited statement; FMP is a third-party
  normalization of it; an LLM readout is probabilistic; yfinance and S-1
  extracts are fallbacks by construction.
* **method_delta** — how the value left the document (`METHOD_DELTA`, keyed
  by `classify_extraction_method` over the fact's ``extracted_by``).
  Deterministic field-mapping is reproducible (+0.02); manual entry is
  human-verified but typo-prone (0.00); an LLM parse is probabilistic
  (−0.05); unknown extractors get a mild haircut (−0.02).
* **issue_penalties** — unresolved ``validation_issues`` rows that target the
  fact (`ISSUE_PENALTY`, halt severity doubles, total capped at
  `MAX_ISSUE_PENALTY`). Cross-source disagreement is the headline signal:
  one unresolved disagreement drops an FMP fact from 0.94 to 0.79 — below
  the UI's low-confidence affordance threshold, by design.
* **self_report** — LLM extractors emit a per-value confidence
  (``KpiValue.confidence``); it multiplies the formula score so the model's
  own uncertainty survives into the stored value. Deterministic writers
  pass None (≡ 1.0).

Worked examples (no unresolved issues):

    sec_official + sec_xbrl            → 0.98 + 0.02         = 1.00
    fmp_normalized + fmp               → 0.92 + 0.02         = 0.94
    fmp_normalized + fmp, disagreement → 0.94 − 0.15         = 0.79
    llm_extracted + llm:* (self 0.95)  → (0.75 − 0.05) × 0.95 = 0.665
    yfinance_fallback + deterministic  → 0.70 + 0.02         = 0.72
    s1_provisional + s1                → 0.65 + 0.02         = 0.67

The scale is documented in directives/data_provenance.md §8. Scoring is
pure and deterministic — same inputs, same float, no clock, no randomness —
so the backfill (`apply_confidence_scores`, exposed via
execution/backfill_confidence.py) is idempotent: a second run over an
unchanged DB updates zero rows.

Ingest-time writers can't see validation issues (validation runs after
persist), so they store the prior ``tier + method`` score; the backfill is
the reconciler that folds issue penalties in after each validation run.

One deliberate non-goal: rows whose ``extracted_by`` is LLM-ish and whose
stored confidence is already ≠ 1.0 were self-scored at extraction time
(model self-report folded in at ingest). The original self-report isn't
recoverable from the stored product, so the backfill PRESERVES those rows
instead of rescoring them — recomputing would either destroy the
self-report signal or break idempotence.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from models.documents import SourceQualityTier

if TYPE_CHECKING:
    from credibility.priors import MeasuredPriors

log = logging.getLogger(__name__)

# Base score by documents.source_quality_tier. Unknown tiers (legacy rows,
# future enum additions not yet mapped) take UNKNOWN_TIER_BASE.
TIER_BASE: dict[str, float] = {
    SourceQualityTier.SEC_OFFICIAL.value: 0.98,
    SourceQualityTier.FMP_NORMALIZED.value: 0.92,
    SourceQualityTier.LLM_EXTRACTED.value: 0.75,
    SourceQualityTier.YFINANCE_FALLBACK.value: 0.70,
    SourceQualityTier.S1_PROVISIONAL.value: 0.65,
}
UNKNOWN_TIER_BASE = 0.70

ExtractionMethod = Literal["deterministic", "manual", "llm", "unknown"]

# extracted_by values produced by mechanical field-mapping extractors —
# reproducible runs over structured payloads, no judgment in the loop.
_DETERMINISTIC_EXTRACTORS: frozenset[str] = frozenset(
    {
        "fmp",
        "fmp_as_reported",
        "sec_xbrl",
        "s1",
        "fmp_derived",
        "kpi_transform_derived",
        "ir_spreadsheet",
    }
)

METHOD_DELTA: dict[ExtractionMethod, float] = {
    "deterministic": 0.02,
    "manual": 0.00,
    "llm": -0.05,
    "unknown": -0.02,
}

# Penalty per unresolved validation_issues row targeting the fact, keyed by
# rule. Halt severity doubles the rule's penalty; the summed penalty is
# capped so a pathological issue pileup can't zero out a fact.
ISSUE_PENALTY: dict[str, float] = {
    "source_disagreement": 0.15,
    "magnitude_jump": 0.10,
    "plausible_range": 0.10,
    "unit_mismatch": 0.10,
}
UNKNOWN_RULE_PENALTY = 0.05
HALT_MULTIPLIER = 2.0
MAX_ISSUE_PENALTY = 0.40

# Floor: a fact we display at all never claims literal-zero confidence.
MIN_CONFIDENCE = 0.05


@dataclass(frozen=True, slots=True)
class IssueSignal:
    """One unresolved validation issue, reduced to what scoring needs."""

    rule: str
    severity: str = "warn"


def classify_extraction_method(extracted_by: str | None) -> ExtractionMethod:
    """Bucket a fact's ``extracted_by`` audit tag into an extraction method.

    ``llm:<model>`` (kpi_persistence's convention), bare ``llm``/
    ``llm_extracted``, and the IR-deck readout are LLM parses; ``manual_*`` and
    analyst-comment captures are human entry; the closed deterministic set is
    mechanical field-mapping. Anything else — including NULL on legacy rows —
    is unknown.
    """
    if extracted_by is None:
        return "unknown"
    tag = extracted_by.strip().lower()
    if tag in _DETERMINISTIC_EXTRACTORS:
        return "deterministic"
    if tag.startswith("llm") or tag == "ir_presentation_readout":
        return "llm"
    if tag.startswith("manual") or tag == "analyst_comment":
        return "manual"
    return "unknown"


def score_confidence(
    *,
    tier: SourceQualityTier | str | None,
    extracted_by: str | None,
    issues: tuple[IssueSignal, ...] | list[IssueSignal] = (),
    self_reported: float | None = None,
    tier_base_override: float | None = None,
) -> float:
    """The documented formula. Pure; rounds to 4 decimals; clamps to
    [MIN_CONFIDENCE, 1.0]. See the module docstring for the worked examples
    (which the tests pin verbatim).

    ``tier_base_override`` (Close-the-Loops L10) replaces the hand-set
    ``TIER_BASE`` lookup with a MEASURED base for this fact's
    (tier × ticker × line-item) cell — supplied by the caller only when the
    credibility ledger has enough graded observations to earn it (see
    ``credibility.priors``). Default None keeps the constant, so every existing
    call site is unchanged."""
    tier_key = tier.value if isinstance(tier, SourceQualityTier) else tier
    base = (
        tier_base_override
        if tier_base_override is not None
        else TIER_BASE.get(tier_key or "", UNKNOWN_TIER_BASE)
    )
    delta = METHOD_DELTA[classify_extraction_method(extracted_by)]

    penalty = 0.0
    for issue in issues:
        rule_penalty = ISSUE_PENALTY.get(issue.rule, UNKNOWN_RULE_PENALTY)
        if issue.severity == "halt":
            rule_penalty *= HALT_MULTIPLIER
        penalty += rule_penalty
    penalty = min(penalty, MAX_ISSUE_PENALTY)

    score = base + delta - penalty
    if self_reported is not None:
        score *= max(0.0, min(1.0, self_reported))
    return round(max(MIN_CONFIDENCE, min(1.0, score)), 4)


# ---------------------------------------------------------------------------
# Matching unresolved validation_issues back to individual fact rows
# ---------------------------------------------------------------------------
#
# validation_issues has no fact_id FK — issues anchor to (ticker,
# source_doc_id) and encode the offending fact in raw_value, in one of the
# validation engine's fixed formats:
#
#   plausible_range (fin)  "revenue=-5"
#   plausible_range (kpi)  "Risk-adjusted NIM=1200 percent"   (also unit_mismatch)
#   magnitude_jump         "revenue prior=100 current=900 (ratio=9.0x) at 2025-12-31"
#   source_disagreement    "revenue @ 2025-12-31: fmp=100 vs sec_xbrl=101 (0.99%)"
#
# _parse_issue lifts (item, period, value) out of those shapes;
# _issue_matches applies them at the precision each rule supports: rules
# embedding a period match on (item, period); value-anchored rules match on
# (item, value). Unparseable raw_values match nothing — a vague issue must
# never blanket-penalize a whole series.


@dataclass(frozen=True, slots=True)
class _ParsedIssue:
    rule: str
    severity: str
    item: str
    period: str | None  # YYYY-MM-DD when the raw_value embeds one
    value: float | None  # offending value when the raw_value embeds one
    raw: str = ""  # original raw_value — feeds the popover display strings


# The grouped unresolved-issues map that ``load_unresolved_issues`` returns and
# ``display_issues_for_fact``/``issues_for_fact`` consume. Public so callers
# (e.g. ``ask.grounding``, which threads it into the answer prompt) can annotate
# it without reaching into the private ``_ParsedIssue`` element type.
IssuesByTicker = dict[str, list[_ParsedIssue]]


def _parse_issue(rule: str, severity: str, raw_value: str) -> _ParsedIssue | None:
    raw = raw_value.strip()
    if " @ " in raw:  # source_disagreement
        item, rest = raw.split(" @ ", 1)
        period = rest[:10] if len(rest) >= 10 else None
        return _ParsedIssue(
            rule=rule, severity=severity, item=item.strip(), period=period, value=None, raw=raw
        )
    if " prior=" in raw:  # magnitude_jump
        item = raw.split(" prior=", 1)[0].strip()
        period = raw[-10:] if " at " in raw else None
        return _ParsedIssue(
            rule=rule, severity=severity, item=item, period=period, value=None, raw=raw
        )
    if "=" in raw:  # plausible_range / unit_mismatch: "<item>=<value>[ <unit>]"
        item, rest = raw.split("=", 1)
        try:
            value = float(rest.split(" ", 1)[0])
        except ValueError:
            return None
        return _ParsedIssue(
            rule=rule, severity=severity, item=item.strip(), period=None, value=value, raw=raw
        )
    return None


def _issue_matches(parsed: _ParsedIssue, *, item: str, period: str, value: float) -> bool:
    if parsed.item != item:
        return False
    if parsed.period is not None:
        return parsed.period == period
    if parsed.value is not None:
        return abs(parsed.value - value) <= max(1e-9, abs(parsed.value) * 1e-9)
    return False


def load_unresolved_issues(conn: sqlite3.Connection) -> dict[str, list[_ParsedIssue]]:
    """Unresolved validation_issues parsed and grouped by ticker. ``{}`` when
    the table is missing (pre-0006 fixture DBs)."""
    try:
        rows = conn.execute(
            "SELECT ticker, rule, severity, raw_value FROM validation_issues "
            "WHERE resolved_at IS NULL AND raw_value IS NOT NULL AND ticker IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, list[_ParsedIssue]] = {}
    for ticker, rule, severity, raw_value in rows:
        parsed = _parse_issue(str(rule), str(severity), str(raw_value))
        if parsed is not None:
            out.setdefault(str(ticker).upper(), []).append(parsed)
    return out


def issues_for_fact(
    by_ticker: dict[str, list[_ParsedIssue]],
    *,
    ticker: str,
    item: str,
    period_end: str,
    value: float,
) -> tuple[IssueSignal, ...]:
    """The fact's matching unresolved issues, ready for `score_confidence`.

    ``item`` is the financial_facts.line_item or kpi_definitions.name;
    ``period_end`` is the ISO date (YYYY-MM-DD) of the fact's period.
    """
    candidates = by_ticker.get(ticker.upper())
    if not candidates:
        return ()
    period = period_end[:10]
    return tuple(
        IssueSignal(rule=p.rule, severity=p.severity)
        for p in candidates
        if _issue_matches(p, item=item, period=period, value=value)
    )


# ---------------------------------------------------------------------------
# Popover display strings for matched issues (S2 PR3)
# ---------------------------------------------------------------------------

# Short labels for the disagreement string, keyed by documents.source_type
# (the engine embeds source_type, not tier, in disagreement raw_values).
_SOURCE_TYPE_SHORT: dict[str, str] = {
    "sec_xbrl": "SEC",
    "fmp": "FMP",
    "ir_doc": "IR",
    "llm_extracted": "LLM",
    "sec_s1": "S-1",
    "transcript_audio": "Call",
    "manual_csv": "Manual",
    "manual_entry": "Manual",
}

# Displayed tier → the source_type its rows come from, to pick which side of
# a disagreement is "the other source" relative to the displayed cell.
_TIER_TO_SOURCE_TYPE: dict[str, str] = {
    SourceQualityTier.SEC_OFFICIAL.value: "sec_xbrl",
    SourceQualityTier.FMP_NORMALIZED.value: "fmp",
    SourceQualityTier.LLM_EXTRACTED.value: "llm_extracted",
    SourceQualityTier.S1_PROVISIONAL.value: "sec_s1",
}

_DISAGREEMENT_RE = re.compile(
    r"(?P<a_st>\w+)=(?P<a_val>-?[\d.eE+]+) vs (?P<b_st>\w+)=(?P<b_val>-?[\d.eE+]+) "
    r"\((?P<pct>[\d.]+)%\)"
)


def _fmt_compact_value(raw: str) -> str:
    """Compact display for a disagreement-side value: $-scaled for big
    actuals ('$101M'), trimmed float otherwise (KPI percents, ratios)."""
    try:
        v = float(raw)
    except ValueError:
        return raw
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:.0f}M"
    if a >= 1e5:
        return f"${v / 1e3:.0f}K"
    return f"{v:g}"


def display_issues_for_fact(
    by_ticker: dict[str, list[_ParsedIssue]],
    *,
    ticker: str,
    item: str,
    period_end: str,
    value: float,
    displayed_tier: str | None = None,
) -> list[str]:
    """Pre-formatted popover strings for the fact's matching unresolved
    issues — what CellSource.issues carries (S2 PR3).

    A disagreement reads from the displayed cell's perspective: the OTHER
    source's claim ("⚠ SEC says $101M, 0.99% delta"); ``displayed_tier``
    picks the side (falls back to naming both when ambiguous). Rules without
    a bespoke format fall back to "⚠ <rule>: <raw_value>" — which is exactly
    where a manual-override reason recorded per data_provenance.md §3
    surfaces.
    """
    candidates = by_ticker.get(ticker.upper())
    if not candidates:
        return []
    period = period_end[:10]
    out: list[str] = []
    displayed_st = _TIER_TO_SOURCE_TYPE.get(displayed_tier or "")
    for p in candidates:
        if not _issue_matches(p, item=item, period=period, value=value):
            continue
        if p.rule == "source_disagreement":
            m = _DISAGREEMENT_RE.search(p.raw)
            if m is not None:
                sides = [(m["a_st"], m["a_val"]), (m["b_st"], m["b_val"])]
                others = [s for s in sides if s[0] != displayed_st] or sides
                if displayed_st is not None and len(others) == 1:
                    st, val = others[0]
                    label = _SOURCE_TYPE_SHORT.get(st, st)
                    out.append(f"⚠ {label} says {_fmt_compact_value(val)}, {m['pct']}% delta")
                else:
                    a_label = _SOURCE_TYPE_SHORT.get(sides[0][0], sides[0][0])
                    b_label = _SOURCE_TYPE_SHORT.get(sides[1][0], sides[1][0])
                    out.append(
                        f"⚠ {a_label} {_fmt_compact_value(sides[0][1])} vs "
                        f"{b_label} {_fmt_compact_value(sides[1][1])} ({m['pct']}% delta)"
                    )
                continue
            out.append(f"⚠ source disagreement: {p.raw}")
        elif p.rule == "magnitude_jump":
            out.append(f"⚠ magnitude jump vs prior period ({p.raw.split(' prior=', 1)[-1]})")
        elif p.rule in ("plausible_range", "unit_mismatch"):
            out.append(f"⚠ {p.rule.replace('_', ' ')}: {p.raw}")
        else:
            out.append(f"⚠ {p.rule.replace('_', ' ')}: {p.raw}")
    return out


# ---------------------------------------------------------------------------
# DB-level rescore (the backfill engine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RescoreOutcome:
    """Per-table tally from `apply_confidence_scores`."""

    table: str
    examined: int
    updated: int
    preserved_self_scored: int


_FACT_SQL = """
SELECT ff.id, ff.ticker, ff.line_item AS item, ff.period_end, ff.value,
       ff.confidence, ff.extracted_by, d.source_quality_tier AS tier
FROM financial_facts ff JOIN documents d ON d.id = ff.source_doc_id
"""

_KPI_SQL = """
SELECT kf.id, kf.ticker, kd.name AS item, kf.period_end, kf.value,
       kf.confidence, kf.extracted_by, d.source_quality_tier AS tier
FROM kpi_facts kf
JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
JOIN documents d ON d.id = kf.source_doc_id
"""


def apply_confidence_scores(
    conn: sqlite3.Connection,
    *,
    table: Literal["financial_facts", "kpi_facts"],
    ticker: str | None = None,
    apply: bool = True,
    priors: MeasuredPriors | None = None,
) -> RescoreOutcome:
    """Recompute the formula for every row of one fact table; UPDATE rows
    whose stored confidence differs. Idempotent — rerunning over an unchanged
    DB updates nothing. ``apply=False`` counts without writing.

    LLM-method rows with a non-default stored confidence are self-scored
    extractions (see the module docstring) and are preserved, tallied in
    ``preserved_self_scored``.

    ``priors`` (Close-the-Loops L10) is the MEASURED prior built from the
    credibility ledger. When supplied, each non-LLM row's tier base is replaced
    by the measured base for its (tier × ticker × line-item) cell — but only
    where that cell has cleared the minimum-n gate; everywhere else
    ``priors.tier_base`` returns the constant, so the score is unchanged. This is
    the one consumer wired to the measured prior as proof; default None keeps the
    pure constant-only behavior.
    """
    sql = _FACT_SQL if table == "financial_facts" else _KPI_SQL
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " WHERE ff.ticker = ?" if table == "financial_facts" else " WHERE kf.ticker = ?"
        params = (ticker.upper(),)
    issues = load_unresolved_issues(conn)

    examined = 0
    preserved = 0
    updates: list[tuple[float, int]] = []
    for row in conn.execute(sql, params):
        row_id, tkr, item, period_end, value, current, extracted_by, tier = (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            float(row[4]),
            float(row[5]),
            row[6],
            row[7],
        )
        examined += 1
        method = classify_extraction_method(str(extracted_by) if extracted_by is not None else None)
        if method == "llm" and current != 1.0:
            preserved += 1
            continue
        tier_str = str(tier) if tier is not None else None
        base_override: float | None = None
        if priors is not None and tier_str is not None:
            fallback = TIER_BASE.get(tier_str, UNKNOWN_TIER_BASE)
            base_override = priors.tier_base(
                tier_str, fallback=fallback, ticker=tkr, line_item=item
            )
        new = score_confidence(
            tier=tier_str,
            extracted_by=str(extracted_by) if extracted_by is not None else None,
            issues=issues_for_fact(
                issues, ticker=tkr, item=item, period_end=period_end, value=value
            ),
            tier_base_override=base_override,
        )
        if abs(new - current) > 1e-9:
            updates.append((new, row_id))

    if apply and updates:
        conn.executemany(f"UPDATE {table} SET confidence = ? WHERE id = ?", updates)
        conn.commit()
    log.info(
        {
            "event": "confidence_rescore",
            "table": table,
            "examined": examined,
            "updated": len(updates),
            "preserved_self_scored": preserved,
            "applied": apply,
        }
    )
    return RescoreOutcome(
        table=table, examined=examined, updated=len(updates), preserved_self_scored=preserved
    )

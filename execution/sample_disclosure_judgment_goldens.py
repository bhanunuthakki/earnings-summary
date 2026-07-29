"""D1e (docs/design/disclosure_intelligence_v1_prd.md): draw a reproducible,
stratified sample of prod ``disclosure_events`` rows for hand-labeling the
two judgment purposes that shipped with zero ground truth —
``metric_lifecycle_triage`` (business-meaningful vs accounting-plumbing +
concealment/maturity priors, over XBRL tag name/label/last-value) and
``disclosure_item_specificity_triage`` (boilerplate vs substantive, over
diff hunks).

READ-ONLY. Opens the DB with ``mode=ro`` (SQLite driver refuses any write)
and expects to be pointed at a copy, never the live prod file directly, per
this repo's prod-DB convention.

Only samples rows the LLM actually classified:

* metric-lifecycle: ``event_type='metric_discontinued'`` rows where
  ``interpretation_md`` is non-NULL (a triage call landed — the mechanical
  ``relabeled``/``standard_transition`` rows never reach the LLM at all, per
  ``execution/detect_discontinued_metrics.py``'s wiring). Those two mechanical
  kinds are ALSO sampled (separately) because the PRD asks for a mix across
  all three subject kinds, but they are reported as a deterministic-classifier
  spot check, not folded into the LLM purpose's own accuracy number.
* specificity: item-level rows where ``interpretation_md`` is neither a
  ``deterministic: ...`` band note nor a degrade sentinel — i.e. rows that
  actually survived to ``disclosure_item_specificity_triage``.

Output: one JSON file with every field a human labeler needs (the LLM's
verdict AND the underlying evidence), stratified per the D1e brief. Never
prints the payload to stdout — written straight to the given ``--out`` path
(hand-labeling happens by editing/annotating that file, not by piping this
script's output anywhere).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.specificity import extract_diff_hunk  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("sample_disclosure_judgment_goldens")

_EVIDENCE_RX = re.compile(r"=\s*([\d,.\-]+)\s+as of\s+(\S+)\s+\[(\w+) axis\]")
_SILENCE_RX = re.compile(r"silent for (\d+) \w+ period\(s\); historical max gap was (\d+)")


@dataclass(slots=True)
class MetricLifecycleSampleRow:
    prod_event_id: int
    ticker: str
    event_type: str
    fiscal_year: int | None
    fiscal_period: str | None
    qualified_name: str
    label: str
    axis: str | None
    last_value: float | None
    last_period_label: str | None
    current_silence: int | None
    historical_max_gap: int | None
    materiality: float | None
    prod_verdict: str
    prod_interpretation_md: str | None


@dataclass(slots=True)
class SpecificitySampleRow:
    prod_event_id: int
    ticker: str
    event_type: str
    canonical_id: str | None
    heading: str
    hunk: str
    prod_verdict: str
    prod_confidence: float | None
    prod_interpretation_md: str | None


def _parse_evidence(evidence_quote: str | None) -> tuple[float | None, str | None, str | None]:
    if not evidence_quote:
        return None, None, None
    m = _EVIDENCE_RX.search(evidence_quote)
    if not m:
        return None, None, None
    raw_val, period_label, axis = m.groups()
    try:
        val = float(raw_val.replace(",", ""))
    except ValueError:
        val = None
    return val, period_label, axis


def _parse_silence(current_excerpt: str | None) -> tuple[int | None, int | None]:
    if not current_excerpt:
        return None, None
    m = _SILENCE_RX.search(current_excerpt)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _fetch_metric_rows(
    conn: sqlite3.Connection, *, event_type: str, verdict: str | None
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    clauses = ["event_type = ?"]
    params: list[object] = [event_type]
    if verdict is not None:
        clauses.append("verdict = ?")
        params.append(verdict)
    else:
        # "LLM ran, business_metric+unclear stayed unclassified" bucket.
        clauses.append("verdict = 'unclassified' AND interpretation_md IS NOT NULL")
    sql = f"SELECT * FROM disclosure_events WHERE {' AND '.join(clauses)}"
    return conn.execute(sql, params).fetchall()


def _to_metric_sample_row(r: sqlite3.Row) -> MetricLifecycleSampleRow:
    val, period_label, axis = _parse_evidence(r["evidence_quote"] or r["prior_excerpt"])
    silence, max_gap = _parse_silence(r["current_excerpt"])
    return MetricLifecycleSampleRow(
        prod_event_id=r["id"],
        ticker=r["ticker"],
        event_type=r["event_type"],
        fiscal_year=r["fiscal_year"],
        fiscal_period=r["fiscal_period"],
        qualified_name=r["subject"],
        label=r["subject_label"] or "",
        axis=axis,
        last_value=val,
        last_period_label=period_label,
        current_silence=silence,
        historical_max_gap=max_gap,
        materiality=r["materiality"],
        prod_verdict=r["verdict"],
        prod_interpretation_md=r["interpretation_md"],
    )


def _fetch_specificity_rows(
    conn: sqlite3.Connection, *, canonical_id: str, verdict: str
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT * FROM disclosure_events
        WHERE event_type IN ('item_added','item_removed','item_reworded')
          AND canonical_id = ?
          AND verdict = ?
          AND interpretation_md IS NOT NULL
          AND interpretation_md NOT LIKE 'deterministic:%'
          AND interpretation_md NOT IN ('llm_triage_degraded', 'llm_triage_missing_verdict')
    """
    return conn.execute(sql, (canonical_id, verdict)).fetchall()


def _hunk_for_row(r: sqlite3.Row) -> str:
    """Mirror ``filings.boilerplate_classify._hunk_for_event`` exactly, so the
    golden set's ``hunk`` field is the SAME text the production LLM call saw."""
    event_type = r["event_type"]
    prior = r["prior_excerpt"]
    current = r["current_excerpt"]
    evidence = r["evidence_quote"]
    if event_type == "item_added":
        return current or evidence or ""
    if event_type == "item_removed":
        return prior or evidence or ""
    hunk = extract_diff_hunk(prior or "", current or "")
    return hunk or evidence or ""


def _to_specificity_sample_row(r: sqlite3.Row) -> SpecificitySampleRow:
    return SpecificitySampleRow(
        prod_event_id=r["id"],
        ticker=r["ticker"],
        event_type=r["event_type"],
        canonical_id=r["canonical_id"],
        heading=r["subject_label"] or r["subject"],
        hunk=_hunk_for_row(r),
        prod_verdict=r["verdict"],
        prod_confidence=r["confidence"],
        prod_interpretation_md=r["interpretation_md"],
    )


@dataclass(slots=True)
class Sample:
    seed: int
    metric_lifecycle: list[dict[str, object]]
    specificity: list[dict[str, object]]


def build_sample(conn: sqlite3.Connection, *, seed: int) -> Sample:
    rng = random.Random(seed)

    metric_plan: list[tuple[str, str | None, int]] = [
        ("metric_discontinued", "concealment", 4),  # all 4 that exist prod-wide
        ("metric_discontinued", "maturity", 6),
        ("metric_discontinued", "noise", 6),
        ("metric_discontinued", None, 4),  # LLM ran, business_metric+unclear
        ("metric_relabeled", "mechanical", 3),
        ("metric_standard_transition", "mechanical", 2),
    ]
    metric_rows: list[MetricLifecycleSampleRow] = []
    for event_type, verdict, n in metric_plan:
        pool = _fetch_metric_rows(conn, event_type=event_type, verdict=verdict)
        picked = pool if len(pool) <= n else rng.sample(pool, n)
        metric_rows.extend(_to_metric_sample_row(r) for r in picked)

    specificity_plan: list[tuple[str, str, int]] = [
        ("risk_factors", "boilerplate_update", 5),
        ("risk_factors", "substantive", 5),
        ("mdna", "boilerplate_update", 7),
        ("mdna", "substantive", 8),
    ]
    specificity_rows: list[SpecificitySampleRow] = []
    for canonical_id, verdict, n in specificity_plan:
        pool = _fetch_specificity_rows(conn, canonical_id=canonical_id, verdict=verdict)
        picked = pool if len(pool) <= n else rng.sample(pool, n)
        specificity_rows.extend(_to_specificity_sample_row(r) for r in picked)

    return Sample(
        seed=seed,
        metric_lifecycle=[asdict(r) for r in metric_rows],
        specificity=[asdict(r) for r in specificity_rows],
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=str,
        required=True,
        help="Path to a COPY of portfolio.db (opened mode=ro regardless).",
    )
    parser.add_argument("--seed", type=int, default=20260725)
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "disclosure_judgment_sample.json",
    )
    args = parser.parse_args(argv)

    conn = connect_sqlite(Path(args.db), role=SQLiteConnectionRole.READ_ONLY)
    try:
        sample = build_sample(conn, seed=args.seed)
    finally:
        conn.close()

    n_metric = len(sample.metric_lifecycle)
    n_specificity = len(sample.specificity)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(asdict(sample), indent=2, default=str), encoding="utf-8")
    log.info(
        {
            "event": "sample_written",
            "path": str(args.out),
            "n_metric_lifecycle": n_metric,
            "n_specificity": n_specificity,
        }
    )
    print(f"wrote {args.out} — {n_metric} metric-lifecycle rows, {n_specificity} specificity rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

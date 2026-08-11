"""Pressure-test a draft thesis against the canonical corpus.

Reads a thesis statement (CLI `--thesis "..."` or from
`micro_thesis/holdings/<TICKER>.json`'s `thesis` field), assembles the
relevant corpus (transcripts, 5y financials, segments, 10-K risk factors),
and calls Claude with a structured prompt that returns:

  - strongest_counter:        the most credible bear case against the thesis
  - contradicting_evidence:   data points in the corpus that work against it
  - mgmt_credibility_check:   pattern of management Say-vs-Do over the period
  - thesis_assumptions:       implicit assumptions the thesis depends on
  - conviction_rating:        high / medium / low + reasoning

Output is written to both:
  - micro_thesis/diligence/<TICKER>.md  (appended as §7 with human-readable summary)
  - .tmp/pressure_tests/<TICKER>_<DATE>.json  (audit trail)

Usage:
    python execution/pressure_test_thesis.py --ticker NVO
    python execution/pressure_test_thesis.py --ticker AMD --thesis "AMD captures 20% of MI3xx datacenter share by FY28..."
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from llm.contracts import PRESSURE_TEST_SCHEMA, PressureTestPayload  # noqa: E402
from llm.structured import call_llm_structured  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    ticker = args.ticker.upper()

    # An explicit --repo-root must also reach the governed LLM cost ledger:
    # `call_llm(purpose="pressure_test_thesis")` resolves the llm_calls DB (plus
    # the budget check and the LLM_MODELS pin) from the `db.DB_PATH` process
    # global, which this script otherwise never syncs — so a run against another
    # checkout's data/ (e.g. --repo-root <MAIN> from a worktree) would silently
    # land the cost row in the default DB. set_db_path also re-points
    # DATA_DIR / FMP_DIR for the corpus reads.
    db.set_db_path(repo_root / "data" / "portfolio.db")

    thesis = _resolve_thesis(args.thesis, repo_root, ticker)
    if not thesis:
        sys.stderr.write(
            f"No thesis found. Pass --thesis '...' or populate "
            f"micro_thesis/holdings/{ticker}.json's 'thesis' field.\n"
        )
        return 1

    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    context_md = _assemble_corpus_md(ticker, repo_root, db_path)
    result = _call_pressure_test(ticker, thesis, context_md)

    audit_path = _write_audit(repo_root, ticker, thesis, result)
    diligence_path = _append_to_diligence(repo_root, ticker, thesis, result)

    print(
        json.dumps(
            {
                "ticker": ticker,
                "audit_path": str(audit_path),
                "diligence_path": str(diligence_path) if diligence_path else None,
                "conviction_rating": result.conviction_rating,
            },
            indent=2,
        )
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Ticker the thesis is about")
    p.add_argument(
        "--thesis",
        help="Thesis statement (overrides micro_thesis/holdings/<TICKER>.json)",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, micro_thesis/. Default: this repo.",
    )
    return p.parse_args(argv)


def _resolve_thesis(cli_thesis: str | None, repo_root: Path, ticker: str) -> str | None:
    if cli_thesis and cli_thesis.strip():
        return cli_thesis.strip()
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not path.exists():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    holdings = cast("dict[str, object]", data)
    for key in ("thesis_full", "thesis"):
        v = holdings.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _assemble_corpus_md(ticker: str, repo_root: Path, db_path: Path) -> str:
    """5y financials + ratios + 10-K risks excerpt + latest transcript summaries.

    Trimmed aggressively so the prompt stays under the model's effective context.
    """
    out = StringIO()
    with connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY) as conn:
        conn.row_factory = sqlite3.Row
        annual = _load_annual(conn, ticker, n=5)
    if annual:
        out.write("### 5-year annual financials\n\n")
        out.write("| FY | Revenue | OI margin | FCF margin | Net margin | ROE |\n")
        out.write("|---|---|---|---|---|---|\n")
        for row in annual:
            revenue = row["revenue"]
            rev_str = f"${revenue / 1_000_000:,.0f}M" if isinstance(revenue, (int, float)) else "—"
            out.write(
                f"| {row['fiscal_year']} | {rev_str} | "
                f"{_pct(row.get('operating_margin'))} | {_pct(row.get('fcf_margin'))} | "
                f"{_pct(row.get('net_margin'))} | {_pct(row.get('roe'))} |\n"
            )
        out.write("\n")

    risks_text, year = _load_risk_factors(repo_root, ticker)
    if risks_text:
        out.write(f"### Risk factors (FY{year or 'latest'} 10-K, 6k-char excerpt)\n\n")
        out.write("```\n")
        out.write(risks_text[:6_000])
        out.write("\n```\n\n")

    transcripts = _load_recent_transcripts(repo_root, ticker, n=4)
    if transcripts:
        out.write(f"### Latest {len(transcripts)} earnings-call summaries\n\n")
        for label, text in transcripts:
            out.write(f"#### {label}\n\n")
            out.write(text[:3_500])
            out.write("\n\n")

    return out.getvalue() or "_(no corpus material available)_\n"


# Direct financial_facts read — NOT the metrics/ratios views. The views
# window-scan all ~726k facts per query (~12s each) and key identity off the
# fiscal-period LABEL, which cross-labels quarters for off-calendar reporters;
# reading facts keyed on the period_end DATE is the #452 pattern (see
# src/report/sections/financials.py, which documents the rationale). Period
# types mirror report.sections._common.ANNUAL_PERIOD_TYPES.
_ANNUAL_FACTS_SQL = """
WITH dedup AS (
    SELECT
        line_item, value,
        CAST(substr(period_end, 1, 4) AS INTEGER) AS fiscal_year,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(substr(period_end, 1, 4) AS INTEGER), line_item
            ORDER BY source_doc_id DESC, period_end DESC
        ) AS rn
    FROM financial_facts
    WHERE ticker = ? AND fiscal_period_type IN ('FY', 'annual')
)
SELECT
    fiscal_year,
    MAX(CASE WHEN line_item = 'revenue' THEN value END) AS revenue,
    MAX(CASE WHEN line_item = 'operating_income' THEN value END) AS operating_income,
    MAX(CASE WHEN line_item = 'net_income' THEN value END) AS net_income,
    MAX(CASE WHEN line_item = 'free_cash_flow' THEN value END) AS free_cash_flow,
    MAX(CASE WHEN line_item = 'total_stockholders_equity' THEN value END) AS total_equity
FROM dedup
WHERE rn = 1
GROUP BY fiscal_year
ORDER BY fiscal_year DESC
LIMIT ?
"""


def _load_annual(conn: sqlite3.Connection, ticker: str, n: int) -> list[dict[str, object]]:
    """Last N fiscal years (oldest first) with the ratios the corpus table shows.

    Margins/ROE are computed here from the underlying line items — same
    formulas as the retired ``ratios`` view (x / NULLIF(denominator, 0)).
    """
    cur = conn.cursor()
    cur.execute(_ANNUAL_FACTS_SQL, (ticker.upper(), n))
    rows: list[dict[str, object]] = []
    for r in cur.fetchall():
        revenue = r["revenue"]
        rows.append(
            {
                "fiscal_year": r["fiscal_year"],
                "revenue": revenue,
                "operating_margin": _ratio(r["operating_income"], revenue),
                "fcf_margin": _ratio(r["free_cash_flow"], revenue),
                "net_margin": _ratio(r["net_income"], revenue),
                "roe": _ratio(r["net_income"], r["total_equity"]),
            }
        )
    rows.reverse()
    return rows


def _ratio(numerator: object, denominator: object) -> float | None:
    if (
        not isinstance(numerator, (int, float))
        or not isinstance(denominator, (int, float))
        or denominator == 0
    ):
        return None
    return float(numerator) / float(denominator)


def _load_risk_factors(repo_root: Path, ticker: str) -> tuple[str, int | None]:
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    candidates = sorted(fmp_dir.glob(f"{ticker}_form_10k_*.json"), reverse=True)
    for path in candidates:
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            continue
        rec = cast("dict[str, object]", payload[0])
        for key in ("item1a", "riskFactors", "risk_factors", "Item 1A"):
            raw = rec.get(key)
            if isinstance(raw, str) and raw.strip():
                year = _year_from_name(path.name)
                return raw.strip(), year
        break
    return "", None


def _year_from_name(name: str) -> int | None:
    for token in name.replace(".", "_").split("_"):
        if token.isdigit() and len(token) == 4 and 2000 <= int(token) <= 2099:
            return int(token)
    return None


def _load_recent_transcripts(repo_root: Path, ticker: str, n: int) -> list[tuple[str, str]]:
    """Pull up to N cached LLM summaries from .tmp/ — chronologically sorted."""
    tmp = repo_root / ".tmp"
    if not tmp.exists():
        return []
    matches: list[tuple[tuple[int, int], Path]] = []
    for path in tmp.glob(f"{ticker}_Q*_summary.txt"):
        parts = path.stem.split("_")
        if len(parts) < 4:
            continue
        try:
            q = int(parts[1].lstrip("Q"))
            year = int(parts[2])
        except ValueError:
            continue
        matches.append(((year, q), path))
    matches.sort(reverse=True)
    out: list[tuple[str, str]] = []
    for (year, q), path in matches[:n]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append((f"Q{q} {year}", text))
    return out


def _call_pressure_test(ticker: str, thesis: str, context_md: str) -> PressureTestPayload:
    """Return a validated pressure-test; provider/repair failures remain exceptions."""
    prompt = f"""You are a senior buy-side analyst stress-testing an investment thesis BEFORE
a portfolio initiation. Your job is to find the weak points, not to agree.

TICKER: {ticker}

DRAFT THESIS:
{thesis}

CORPUS (verbatim excerpts from financials, ratios, 10-K risk factors, recent
earnings-call summaries):

{context_md}

Return strictly a JSON object with these exact keys (no markdown, no commentary):

{{
  "strongest_counter": "Consensus demand assumptions exceed evidence in FY2025 results.",
  "contradicting_evidence": [
    "FY2025 revenue growth slowed to 3%."
  ],
  "evidence_gap": null,
  "mgmt_credibility_check": "Management missed its prior margin commitment.",
  "thesis_assumptions": [
    "Revenue growth reaccelerates above 10%."
  ],
  "conviction_rating": "low",
  "conviction_reasoning": "The evidence directly contradicts the growth premise."
}}

Be specific. Avoid generic risks like 'macro deteriorates'. Every claim should cite
something in the CORPUS or admit it's not yet supported by the data.
"""
    decoded = call_llm_structured(
        prompt,
        purpose="pressure_test_thesis",
        ticker=ticker,
        expect="object",
        schema=PRESSURE_TEST_SCHEMA,
        max_escalation_tier=0,
    )
    if not isinstance(decoded, PressureTestPayload):  # pragma: no cover - schema invariant
        raise TypeError("pressure_test_thesis schema returned an unexpected value")
    return decoded


def _write_audit(repo_root: Path, ticker: str, thesis: str, result: PressureTestPayload) -> Path:
    """Persist a dated JSON snapshot for audit/replay."""
    audit_dir = repo_root / ".tmp" / "pressure_tests"
    audit_dir.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S")
    path = audit_dir / f"{ticker}_{run_date}.json"
    path.write_text(
        json.dumps(
            {
                "ticker": ticker,
                "tested_at": run_date,
                "thesis": thesis,
                "result": result.model_dump(mode="json"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _append_to_diligence(
    repo_root: Path, ticker: str, thesis: str, result: PressureTestPayload
) -> Path | None:
    """Append a §7 Thesis pressure-test section to the diligence markdown.

    Returns None if the diligence file doesn't exist. The legacy
    `build_diligence.py` that used to create it was retired 2026-07-25 (PRD
    §8.3, superseded by the Investment Decision Card), so on a fresh install
    this leg is simply inert — the pressure-test result still lands in its
    own artifact; only this optional markdown append is skipped.
    """
    path = repo_root / "micro_thesis" / "diligence" / f"{ticker}.md"
    if not path.exists():
        return None
    body = StringIO()
    body.write("\n---\n\n## §7 Thesis pressure-test\n\n")
    body.write(f"_Tested {date.today().isoformat()}._\n\n")
    body.write(f"**Thesis under test:**\n\n> {thesis}\n\n")
    body.write(f"**Conviction:** {result.conviction_rating} — {result.conviction_reasoning}\n\n")
    body.write(f"**Strongest counter:** {result.strongest_counter}\n\n")
    if result.contradicting_evidence:
        body.write("**Contradicting evidence:**\n\n")
        for item in result.contradicting_evidence:
            body.write(f"- {item}\n")
        body.write("\n")
    if result.evidence_gap:
        body.write(f"**Evidence gap:** {result.evidence_gap}\n\n")
    body.write(f"**Management credibility:** {result.mgmt_credibility_check}\n\n")
    body.write("**Thesis assumptions to monitor:**\n\n")
    for item in result.thesis_assumptions:
        body.write(f"- {item}\n")
    body.write("\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(body.getvalue())
    return path


def _pct(v: object) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{float(v) * 100:+.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())

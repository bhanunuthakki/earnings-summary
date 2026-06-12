"""Show LLM spend, cache effectiveness, and latency from the llm_calls ledger.

Reads ``data/portfolio.db.llm_calls`` (populated by ``src/llm_client.py`` via
the Phase 0 ledger writer). Renders six sections to stdout:

  1. Window summary    — total spend, calls, cache hit rate, error count
  2. By purpose        — cost + calls + cache-hit % + avg latency per LLM_MODELS key
  3. By model          — cost + calls per model id
  4. By ticker         — top-15 tickers by spend in the window
  5. Latency           — p50 / p95 / max ms per purpose
  6. Dedup candidates  — prompts with the same sha256 hit multiple times
                         (each repeat is a candidate for Phase 1 artifact-cache)
  7. Recent errors     — last 10 rows with non-NULL error
  8. Fallback summary  — Gemini-fallback frequency and which purposes triggered it

Usage:
    python execution/show_llm_spend.py
    python execution/show_llm_spend.py --since 7
    python execution/show_llm_spend.py --since 30 --json
    python execution/show_llm_spend.py --run-id <uuid>     # single-run breakdown

The default window is the last 7 days. Pass ``--since 0`` for all-time.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.stderr.write(f"DB not found at {db_path}\n")
        sys.exit(2)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Verify the ledger table exists — gives a clearer error than a SQL failure.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "llm_calls" not in tables:
        sys.stderr.write("llm_calls table missing — run `python -m alembic upgrade head` first.\n")
        sys.exit(3)
    return conn


def _window_clause(since_days: int, run_id: str | None) -> tuple[str, list[Any]]:
    where_parts: list[str] = []
    params: list[Any] = []
    if since_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        where_parts.append("called_at >= ?")
        params.append(cutoff)
    if run_id:
        where_parts.append("run_id = ?")
        params.append(run_id)
    where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    return where, params


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "-"
    if v < 0.01:
        return f"${v:.4f}"
    return f"${v:.2f}"


def _fmt_pct(num: int | float | None, denom: int | float | None) -> str:
    if not denom or denom == 0 or num is None:
        return "-"
    return f"{100.0 * num / denom:5.1f}%"


def _summary(conn: sqlite3.Connection, where: str, params: list[Any]) -> dict[str, Any]:
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS calls,
            SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END) AS ok_calls,
            SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS err_calls,
            SUM(cost_estimate_usd) AS cost_usd,
            SUM(input_tokens) AS input_tok,
            SUM(cache_creation_input_tokens) AS cache_create_tok,
            SUM(cache_read_input_tokens) AS cache_read_tok,
            SUM(output_tokens) AS output_tok,
            SUM(CASE WHEN fallback_used IS NOT NULL THEN 1 ELSE 0 END) AS fallback_calls,
            COUNT(DISTINCT prompt_sha256) AS distinct_prompts
        FROM llm_calls {where}
        """,
        params,
    ).fetchone()
    return dict(row) if row else {}


def _by_group(
    conn: sqlite3.Connection, where: str, params: list[Any], group_col: str, limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            COALESCE({group_col}, '(none)') AS group_key,
            COUNT(*) AS calls,
            SUM(cost_estimate_usd) AS cost_usd,
            SUM(cache_read_input_tokens) AS cache_read_tok,
            SUM(cache_read_input_tokens) + SUM(cache_creation_input_tokens) + SUM(input_tokens)
                AS total_input_tok,
            AVG(elapsed_ms) AS avg_ms
        FROM llm_calls {where}
        GROUP BY {group_col}
        ORDER BY cost_usd DESC NULLS LAST
        LIMIT {int(limit)}
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile. ``pct`` is 0-100. Returns None on empty."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _latency(conn: sqlite3.Connection, where: str, params: list[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT COALESCE(purpose, '(none)') AS purpose, elapsed_ms
        FROM llm_calls {where} ORDER BY purpose
        """,
        params,
    ).fetchall()
    by_purpose: dict[str, list[float]] = {}
    for r in rows:
        by_purpose.setdefault(r["purpose"], []).append(float(r["elapsed_ms"]))
    out: list[dict[str, Any]] = []
    for purpose, vals in by_purpose.items():
        out.append(
            {
                "purpose": purpose,
                "calls": len(vals),
                "p50_ms": int(_percentile(vals, 50) or 0),
                "p95_ms": int(_percentile(vals, 95) or 0),
                "max_ms": int(max(vals)),
            }
        )
    out.sort(key=lambda r: -r["p95_ms"])
    return out


def _dedup_candidates(
    conn: sqlite3.Connection, where: str, params: list[Any], min_repeats: int = 2, limit: int = 20
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            prompt_sha256,
            COUNT(*) AS hits,
            SUM(cost_estimate_usd) AS total_cost,
            COALESCE(MIN(purpose), '(none)') AS purpose_first,
            COALESCE(MIN(ticker), '(none)') AS ticker_first,
            MAX(prompt_chars) AS prompt_chars
        FROM llm_calls {where}
        GROUP BY prompt_sha256
        HAVING COUNT(*) >= {int(min_repeats)}
        ORDER BY total_cost DESC NULLS LAST
        LIMIT {int(limit)}
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _recent_errors(
    conn: sqlite3.Connection, where: str, params: list[Any], limit: int = 10
) -> list[dict[str, Any]]:
    head_where = where if where else " WHERE 1=1"
    rows = conn.execute(
        f"""
        SELECT id, called_at, purpose, ticker, model, fallback_used, error
        FROM llm_calls {head_where} AND error IS NOT NULL
        ORDER BY called_at DESC
        LIMIT {int(limit)}
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _fallbacks(conn: sqlite3.Connection, where: str, params: list[Any]) -> list[dict[str, Any]]:
    head_where = where if where else " WHERE 1=1"
    rows = conn.execute(
        f"""
        SELECT COALESCE(purpose, '(none)') AS purpose,
               COUNT(*) AS fallback_calls,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS fallback_errors
        FROM llm_calls {head_where} AND fallback_used IS NOT NULL
        GROUP BY purpose
        ORDER BY fallback_calls DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _render_human(report: dict[str, Any], window_label: str) -> str:
    out: list[str] = []
    s = report["summary"]
    out.append(f"=== LLM SPEND - {window_label} ===")
    out.append("")
    out.append(f"Calls:                 {s.get('calls') or 0}")
    out.append(f"  ok:                  {s.get('ok_calls') or 0}")
    out.append(f"  errored:             {s.get('err_calls') or 0}")
    out.append(f"Total cost:            {_fmt_usd(s.get('cost_usd'))}")
    out.append(f"Distinct prompt sha:   {s.get('distinct_prompts') or 0}")
    dup_rate = None
    calls = s.get("calls") or 0
    distinct = s.get("distinct_prompts") or 0
    if calls and distinct:
        dup_rate = 1.0 - (distinct / calls)
    out.append(
        f"Dedup potential:       {_fmt_pct(dup_rate * 100 if dup_rate else 0, 100)} of calls had a repeat sha"
    )
    out.append("")
    cache_read = s.get("cache_read_tok") or 0
    cache_create = s.get("cache_create_tok") or 0
    fresh_input = s.get("input_tok") or 0
    total_input = cache_read + cache_create + fresh_input
    out.append("Input token mix:")
    out.append(f"  cache_read:          {cache_read:>12,} ({_fmt_pct(cache_read, total_input)})")
    out.append(
        f"  cache_creation:      {cache_create:>12,} ({_fmt_pct(cache_create, total_input)})"
    )
    out.append(f"  fresh:               {fresh_input:>12,} ({_fmt_pct(fresh_input, total_input)})")
    out.append(f"Output tokens:         {s.get('output_tok') or 0:>12,}")
    out.append(f"Gemini fallbacks:      {s.get('fallback_calls') or 0}")
    out.append("")

    out.append("--- By purpose (top by cost) ---")
    out.append(
        f"  {'purpose':<28s} {'calls':>6s} {'cost':>10s} {'cache_read%':>12s} {'avg_ms':>8s}"
    )
    for r in report["by_purpose"][:20]:
        cache_pct = _fmt_pct(r.get("cache_read_tok") or 0, r.get("total_input_tok") or 0)
        out.append(
            f"  {r['group_key']:<28s} {r['calls']:>6d} {_fmt_usd(r.get('cost_usd')):>10s} {cache_pct:>12s} {int(r.get('avg_ms') or 0):>8d}"
        )
    out.append("")

    out.append("--- By model ---")
    out.append(f"  {'model':<32s} {'calls':>6s} {'cost':>10s}")
    for r in report["by_model"][:10]:
        out.append(f"  {r['group_key']:<32s} {r['calls']:>6d} {_fmt_usd(r.get('cost_usd')):>10s}")
    out.append("")

    if report["by_ticker"]:
        out.append("--- By ticker (top 15) ---")
        out.append(f"  {'ticker':<10s} {'calls':>6s} {'cost':>10s}")
        for r in report["by_ticker"][:15]:
            out.append(
                f"  {r['group_key']:<10s} {r['calls']:>6d} {_fmt_usd(r.get('cost_usd')):>10s}"
            )
        out.append("")

    out.append("--- Latency (p50 / p95 / max ms) ---")
    out.append(f"  {'purpose':<28s} {'calls':>6s} {'p50':>8s} {'p95':>8s} {'max':>8s}")
    for r in report["latency"]:
        out.append(
            f"  {r['purpose']:<28s} {r['calls']:>6d} {r['p50_ms']:>8d} {r['p95_ms']:>8d} {r['max_ms']:>8d}"
        )
    out.append("")

    if report["dedup"]:
        out.append("--- Dedup candidates (same prompt sha hit >= 2x) ---")
        out.append(
            f"  {'sha':<10s} {'hits':>5s} {'cost':>10s} {'purpose':<20s} {'ticker':<8s} {'chars':>7s}"
        )
        for r in report["dedup"]:
            out.append(
                f"  {r['prompt_sha256'][:10]:<10s} {r['hits']:>5d} {_fmt_usd(r.get('total_cost')):>10s} {r['purpose_first']:<20s} {r['ticker_first']:<8s} {int(r['prompt_chars'] or 0):>7d}"
            )
        out.append("")

    if report["fallbacks"]:
        out.append("--- Gemini fallbacks by purpose ---")
        for r in report["fallbacks"]:
            out.append(
                f"  {r['purpose']:<28s} {r['fallback_calls']:>4d}  errors={r['fallback_errors']}"
            )
        out.append("")

    if report["recent_errors"]:
        out.append("--- Recent errors (last 10) ---")
        for r in report["recent_errors"]:
            msg = (r.get("error") or "")[:120]
            tail = "..." if r.get("error") and len(r["error"]) > 120 else ""
            out.append(
                f"  [{r['called_at'][:19]}] {r.get('purpose') or '?':<20s} {r.get('ticker') or '?':<6s} fb={r.get('fallback_used') or '-':<6s} {msg}{tail}"
            )

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=int, default=7, help="Days to look back (0 = all time)")
    parser.add_argument("--run-id", default=None, help="Filter to a single run_id")
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "portfolio.db"),
        help="Path to portfolio.db (default: this project's DB)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of formatted text")
    args = parser.parse_args()

    conn = _open_db(Path(args.db))
    try:
        where, params = _window_clause(args.since, args.run_id)
        report = {
            "summary": _summary(conn, where, params),
            "by_purpose": _by_group(conn, where, params, "purpose", limit=50),
            "by_model": _by_group(conn, where, params, "model", limit=20),
            "by_ticker": _by_group(conn, where, params, "ticker", limit=50),
            "latency": _latency(conn, where, params),
            "dedup": _dedup_candidates(conn, where, params, min_repeats=2, limit=20),
            "recent_errors": _recent_errors(conn, where, params, limit=10),
            "fallbacks": _fallbacks(conn, where, params),
        }
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        label = (f"last {args.since} day(s)" if args.since > 0 else "all time") + (
            f" · run_id={args.run_id}" if args.run_id else ""
        )
        print(_render_human(report, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())

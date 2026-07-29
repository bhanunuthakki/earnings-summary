"""Ask conversational turn-latency baseline (close_the_loops L14).

The conversational transport is a COLD ``claude -p`` subprocess that re-encodes
the whole thread + evidence every turn, and we verified the CLI envelope exposes
NO way to cache a caller-supplied prompt prefix (see src/ask/turn_cache.py for
the full finding). So the LLM-generation half of a turn is irreducible from the
client side and cannot be timed here without spending. What CAN be cut is the
per-turn work that runs BEFORE pass-1 streams a token:

  * the pack-router round-trip (a fast-model call on every narrative turn),
  * re-retrieval: re-reading + re-parsing + re-squashing the filing JSONs and
    transcript files on every turn (question-independent, yet redone each turn),
  * and, on an exact-repeat turn, the whole retrieval pass.

This harness measures that pre-LLM retrieval path deterministically (no browser,
NO LLM calls — the router is stubbed and the DB carries no tracked companies, so
nothing can spend). It builds a representative synthetic corpus (one dense
filing JSON + one long transcript), then times ``gather_evidence`` and the
cached parse cold (caches cleared) vs warm (corpus cache hot) vs memo-hit
(an exact-repeat turn served from the gather memo), reporting min / p50 / p95 /
max wall-clock ms over N warm runs (nearest-rank percentile, matching
render_latency_baseline.py). It also prints the per-turn LLM round-trip
accounting the caches reduce, since that — not the local ms below — is where the
production latency actually lives (each avoided ``claude -p`` / fast-model
round-trip is a whole subprocess + model call, ~0.5-3s, not captured here).

ASCII-only: this docstring is argparse's --help and the Windows console is cp1252.

Usage:
    python execution/ask_latency_baseline.py
    python execution/ask_latency_baseline.py --runs 9 --sections 60 --lines 1200
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ask import grounding, turn_cache  # noqa: E402
from ask.router import PackRoute  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_TICKER = "TST"


def _nearest_rank(values: list[float], q: float) -> float:
    """Nearest-rank percentile -- always an observed value (the rule
    render_latency_baseline.py and GET /api/metrics/panel both use)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def _time(fn: Callable[[], object], runs: int) -> list[float]:
    """``runs`` timed calls in ms (no warm-up here -- the caller controls cold
    vs warm by clearing the caches between phases). Keeps a reference to each
    result so the call can't be optimized away."""
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
        _ = result
    return samples


def _build_corpus(root: Path, *, sections: int, lines: int) -> tuple[Path, list[str]]:
    """A self-contained synthetic repo: a tracked-company-less DB (so the router
    auto-gates -- zero spend) plus one dense filing JSON + one long transcript on
    disk, registered in ``documents`` / ``transcripts`` so the retrieval scans
    reach them. Returns (db_path, question_terms_present)."""
    (root / "data" / "historical" / "fmp").mkdir(parents=True, exist_ok=True)
    (root / "transcripts" / "processed").mkdir(parents=True, exist_ok=True)

    filing_rel = f"data/historical/fmp/{_TICKER}_form_10k_2025.json"
    transcript_rel = f"transcripts/processed/{_TICKER}_Q1_2026.txt"

    # A dense filing: many sections, each a few hundred words of plausible prose.
    payload: dict[str, object] = {"symbol": _TICKER, "period": "FY", "year": 2025}
    filler = (
        "The company continues to invest in the platform while managing credit "
        "quality, take rate, deposit funding cost, and operating leverage across "
        "every cohort and geography it serves in the period under review. "
    )
    for i in range(sections):
        payload[f"Section {i:02d} Risk and Results"] = [
            {f"Item {i}": [filler * 4, f"Margin and NPL commentary for block {i}."]}
        ]
    (root / filing_rel).write_text(json.dumps(payload), encoding="utf-8")

    # A long transcript: many substantive lines.
    body: list[str] = []
    for i in range(lines):
        body.append(
            f"CFO: In line {i:04d} we discussed margin trajectory, NPL formation, "
            f"deposit costs, and the operating leverage we expect as volumes scale."
        )
    (root / transcript_rel).write_text("\n".join(body), encoding="utf-8")

    db_path = root / "data" / "portfolio.db"
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.SNAPSHOT_DESTINATION)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, source_type TEXT,
            doc_type TEXT, file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP,
            fetch_status TEXT, source_url TEXT, source_quality_tier TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER, ticker TEXT,
            call_date TIMESTAMP, fiscal_period_type TEXT, period_end TIMESTAMP);
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, period_end TIMESTAMP,
            fiscal_period_type TEXT, line_item TEXT, value TEXT,
            unit TEXT DEFAULT 'actual', source_doc_id INTEGER);
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT,
            unit TEXT DEFAULT 'actual');
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, period_end TIMESTAMP,
            fiscal_period_type TEXT, kpi_definition_id INTEGER, value TEXT,
            unit TEXT DEFAULT 'actual', source_doc_id INTEGER);
        """
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url, source_quality_tier)"
        " VALUES (2, ?, 'fmp', 'fmp_10k_json', ?, 'b', '2026-01-05 10:00:00', 'ok', NULL, NULL)",
        (_TICKER, filing_rel),
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url, source_quality_tier)"
        " VALUES (3, ?, 'transcript_audio', 'earnings_call_transcript', ?, 'c',"
        " '2026-04-05 10:00:00', 'ok', NULL, NULL)",
        (_TICKER, transcript_rel),
    )
    conn.execute(
        "INSERT INTO transcripts (document_id, ticker, fiscal_period_type, period_end)"
        " VALUES (3, ?, 'Q1', '2026-03-31')",
        (_TICKER,),
    )
    conn.commit()
    conn.close()
    return db_path, ["margin", "npl", "deposit", "leverage"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ask conversational turn-latency baseline (retrieval reuse, L14)."
    )
    parser.add_argument("--runs", type=int, default=7, help="timed builds per phase (default 7)")
    parser.add_argument("--sections", type=int, default=40, help="filing sections (default 40)")
    parser.add_argument("--lines", type=int, default=800, help="transcript lines (default 800)")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="es_ask_latency_") as tmp:
        root = Path(tmp)
        db_path, _terms = _build_corpus(root, sections=args.sections, lines=args.lines)

        # Belt-and-suspenders: the synthetic DB has no tracked companies (the
        # router auto-gates), and we ALSO stub the router so this can never spend.
        grounding.route_packs = lambda _q, **_k: PackRoute((), "gated", "harness")  # type: ignore[assignment]

        question = "How did margin, NPL formation, and deposit leverage trend this period?"
        scope = [_TICKER]

        def gather(*, cache_key: str | None) -> object:
            return grounding.gather_evidence(
                question, repo_root=root, db_path=db_path, scope_tickers=scope, cache_key=cache_key
            )

        # Phase 1 -- COLD: caches cleared each run, so every run re-reads + re-parses
        # + re-squashes the whole corpus (the pre-L14 per-turn behavior).
        def cold() -> object:
            turn_cache.clear_all()
            return gather(cache_key=None)

        # Phase 2 -- WARM CORPUS: caches cleared ONCE, then re-retrieval reuses the
        # cached parse (only keyword scoring re-runs). A different question each
        # turn never hits the gather memo, so this isolates the corpus-cache win.
        turn_cache.clear_all()
        gather(cache_key=None)  # prime the corpus cache

        def warm_corpus() -> object:
            return gather(cache_key=None)

        # Phase 3 -- GATHER MEMO HIT: an exact-repeat turn in the same thread is
        # served wholesale from the memo (router + every scan skipped).
        turn_cache.clear_all()
        gather(cache_key="sid:bench")  # prime the gather memo

        def memo_hit() -> object:
            return gather(cache_key="sid:bench")

        phases: list[tuple[str, Callable[[], object]]] = [
            ("retrieval COLD (re-parse)", cold),
            ("retrieval WARM (corpus hit)", warm_corpus),
            ("retrieval MEMO (exact repeat)", memo_hit),
        ]

        print(
            f"ask-latency baseline   sections={args.sections}  "
            f"transcript_lines={args.lines}  runs={args.runs}"
        )
        print(f"{'phase':32s}  {'min':>8s}  {'p50':>8s}  {'p95':>8s}  {'max':>8s}   (ms)")
        print("-" * 78)
        p50: dict[str, float] = {}
        for label, fn in phases:
            s = _time(fn, args.runs)
            p50[label] = _nearest_rank(s, 0.50)
            print(
                f"{label:32s}  {min(s):8.2f}  {_nearest_rank(s, 0.50):8.2f}  "
                f"{_nearest_rank(s, 0.95):8.2f}  {max(s):8.2f}"
            )

        cold_p50 = p50["retrieval COLD (re-parse)"]
        warm_p50 = p50["retrieval WARM (corpus hit)"]
        memo_p50 = p50["retrieval MEMO (exact repeat)"]
        print("-" * 78)
        if cold_p50 > 0:
            print(
                f"corpus cache: warm p50 is {warm_p50 / cold_p50 * 100:.0f}% of cold "
                f"({cold_p50 - warm_p50:+.2f} ms/turn saved on re-retrieval)"
            )
            print(
                f"gather memo:  exact-repeat p50 is {memo_p50 / cold_p50 * 100:.0f}% of cold "
                f"({cold_p50 - memo_p50:+.2f} ms/turn)"
            )
        print(f"cache stats: {json.dumps(turn_cache.stats())}")

        print()
        print("Per-turn LLM round-trips (the dominant, un-timed-here latency):")
        print("  armed narrative turn = router(1) + pass-1(1) + followup(<=2) + claims(1)")
        print("  L14 removes: router round-trip on a REPEATED question (route cache);")
        print("               ALL retrieval incl. the router on an EXACT-repeat turn")
        print("               (gather memo). Each avoided round-trip is a whole")
        print("               `claude -p` / fast-model subprocess (~0.5-3s in prod).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

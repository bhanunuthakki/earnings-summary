"""Render-latency baseline for the three heaviest command-center panels.

S9 owns this baseline (directive interaction_paradigm_2026_06 section 6): it
gates the deferred Wave-4 "signals spine" (S12), which is only built IF a
profiled render still misses target. Without a recorded baseline the
"profile first" gate is unenforceable -- so this captures one deterministically
(no browser), and S12 re-runs it to compare.

It times the SERVER-side build of the three panels the directive names --
cockpit, discovery, provenance -- against the real DB, reporting
min / p50 / p95 / max wall-clock ms over N warm runs (nearest-rank percentile,
matching GET /api/metrics/panel). The cockpit matters most here: it is rendered
inline at boot (GET /), so -- unlike the lazily fetched discovery and
provenance panels -- it is NOT captured by the client panel-metrics telemetry
(/api/metrics/panel, surfaced in System -> Data Cache). This is the only place
its server render cost is measured.

ASCII-only: this docstring is argparse's --help and the Windows console is cp1252.

Usage:
    python execution/render_latency_baseline.py
    python execution/render_latency_baseline.py --repo-root . --runs 9
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.discovery_panel import render_discovery_panel  # noqa: E402
from pipeline.provenance_panel import render_provenance_panel  # noqa: E402
from pipeline.research_cockpit import build_cockpit_rows, render_research_cockpit  # noqa: E402


def _nearest_rank(values: list[float], q: float) -> float:
    """Nearest-rank percentile -- always an observed value (same rule the
    GET /api/metrics/panel aggregate uses)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def _time_panel(fn: Callable[[], str], runs: int) -> list[float]:
    """One warm-up build (import + first-touch disk caches), then ``runs`` timed
    builds in ms. Touches the output so the build can't be elided."""
    warm = fn()
    _ = len(warm)
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        html = fn()
        elapsed = (time.perf_counter() - t0) * 1000.0
        _ = len(html)  # force the build to materialize
        samples.append(elapsed)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render-latency baseline for the cockpit / discovery / provenance panels."
    )
    parser.add_argument(
        "--repo-root", default=".", help="repo root holding data/portfolio.db (default: cwd)"
    )
    parser.add_argument("--runs", type=int, default=7, help="timed builds per panel (default 7)")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"no DB at {db_path}", file=sys.stderr)
        return 1

    def cockpit() -> str:
        # Mirrors GET / : open -> build_cockpit_rows -> close -> render. The
        # cockpit ships inline in the shell, so this server cost never reaches
        # the client panel-metrics telemetry.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = build_cockpit_rows(conn, repo_root)
        finally:
            conn.close()
        return render_research_cockpit(rows)

    panels: list[tuple[str, Callable[[], str]]] = [
        ("cockpit (inline at boot)", cockpit),
        ("discovery", lambda: render_discovery_panel(db_path)),
        ("provenance", lambda: render_provenance_panel(db_path, repo_root)),
    ]

    print(f"render-latency baseline   repo_root={repo_root}   runs={args.runs}")
    print(f"{'panel':28s}  {'min':>8s}  {'p50':>8s}  {'p95':>8s}  {'max':>8s}   (ms)")
    print("-" * 72)
    for label, fn in panels:
        try:
            s = _time_panel(fn, args.runs)
        except Exception as exc:  # a broken panel must not abort the others
            print(f"{label:28s}  ERROR: {type(exc).__name__}: {exc}")
            continue
        print(
            f"{label:28s}  {min(s):8.1f}  {_nearest_rank(s, 0.50):8.1f}  "
            f"{_nearest_rank(s, 0.95):8.1f}  {max(s):8.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

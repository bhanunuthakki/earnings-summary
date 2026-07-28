"""Render-latency baseline for the heaviest command-center panels.

S9 owns this baseline (directive interaction_paradigm_2026_06 section 6): it
gates the deferred Wave-4 "signals spine" (S12), which is only built IF a
profiled render still misses target. Without a recorded baseline the
"profile first" gate is unenforceable -- so this captures one deterministically
(no browser), and S12 re-runs it to compare.

It times the SERVER-side build of each panel against the real DB, reporting
min / p50 / p95 / max wall-clock ms over N warm runs (nearest-rank percentile,
matching GET /api/metrics/panel). The panels:

  * "cockpit (inline at boot)" -- build_cockpit_rows + render, the holdings
    grid. The S9 number tracked here for continuity.
  * "inbox (home rail)" -- collect_inbox + render_inbox_stream, the Home rail
    GET / ALSO renders inline. S12 fixed its measured offenders (the queued-
    action N+1, the live-tracker network call, the thesis/news re-scans), so
    this panel is where those fixes show up.
  * "boot (GET / full inline)" -- the WHOLE inline render GET / performs before
    first paint: cockpit + coverage strip + inbox rail + overview + shell. The
    true load-bearing path. cockpit + inbox individually understate it.
  * "discovery" / "provenance" -- the two heaviest LAZILY-fetched panels (their
    server cost DOES reach /api/metrics/panel; included for reference).

The inline-at-boot panels matter most: they are NOT captured by the client
panel-metrics telemetry (/api/metrics/panel, System -> Data Cache), so this is
the only place their server render cost is measured.

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
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dashboard.inbox import collect_inbox, render_inbox_stream  # noqa: E402
from dashboard.upcoming import render_upcoming_strip  # noqa: E402
from pipeline.command_center_shell import render_overview_panel, render_shell  # noqa: E402
from pipeline.discovery_panel import render_discovery_panel  # noqa: E402
from pipeline.provenance_panel import render_provenance_panel  # noqa: E402
from pipeline.research_cockpit import (  # noqa: E402
    CockpitRow,
    build_cockpit_rows,
    render_research_cockpit,
)
from pipeline.tier_runner import tier_coverage_summary  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


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

    def _cockpit_rows() -> dict[str, list[CockpitRow]]:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            return build_cockpit_rows(conn, repo_root)
        finally:
            conn.close()

    def cockpit() -> str:
        # The holdings grid: open -> build_cockpit_rows -> close -> render.
        return render_research_cockpit(_cockpit_rows())

    def inbox() -> str:
        # The Home rail GET / renders inline beside the cockpit (the same
        # collect_inbox + render_inbox_stream call the dashboard route makes).
        return render_inbox_stream(
            collect_inbox(db_path, limit=14),
            db_path=db_path,
            compact=True,
            surface="home",
            show_filters=True,
        )

    def boot() -> str:
        # The COMPLETE inline render GET / performs before first paint -- the
        # load-bearing path the S12 gate cares about. Mirrors the dashboard_page
        # handler: cockpit rows + tier coverage + inbox rail + upcoming strip ->
        # overview panel -> shell.
        rows = _cockpit_rows()
        coverage = tier_coverage_summary(repo_root)
        inbox_html = render_inbox_stream(
            collect_inbox(db_path, limit=14),
            db_path=db_path,
            compact=True,
            surface="home",
            show_filters=True,
        )
        upcoming_html = render_upcoming_strip(db_path, datetime.now(UTC).date())
        overview = render_overview_panel(
            rows,
            coverage,
            inbox_html=inbox_html,
            upcoming_html=upcoming_html,
        )
        return render_shell(overview_html=overview)

    panels: list[tuple[str, Callable[[], str]]] = [
        ("cockpit (inline at boot)", cockpit),
        ("inbox (home rail)", inbox),
        ("boot (GET / full inline)", boot),
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

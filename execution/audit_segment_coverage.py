"""Audit base-FY segment coverage across the whole DCF roster.

FMP's product-segmentation occasionally drops a chunk of a company's segments in the
latest quarter (or a quarter goes missing from the segmentation file). The redesigned
per-segment DCF (``execution/build_redesigned_dcf.py``) derives its modeled segments
from that latest quarter, so a dropped segment makes the DCF silently undercount base
revenue and value a fraction of the company. ``src/dcf/segment_coverage.py`` now guards
against this by falling back to a whole-company build when the modeled segments cover
materially less than the base-year income total.

This script runs the real builder for every roster name (so overrides + fiscal-cadence
detection are exercised exactly as in production), parses the ``cov=`` field off the
RESULT line, and reports which names fall below the coverage floor — the names the guard
downgrades to whole-company. It is read-only: each build writes a throwaway workbook to a
temp dir and nothing touches ``dcf/`` or the DB.

Usage:
  python execution/audit_segment_coverage.py --repo-root <DATA_REPO> [--floor 0.85] [--workers 6]
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
BUILDER = SCRIPT_DIR / "build_redesigned_dcf.py"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf.segment_coverage import COVERAGE_FLOOR  # noqa: E402
from runtime.python_process import managed_python_prefix  # noqa: E402

# Non-ticker workbook stems to ignore when scanning dcf/*.xlsx.
_SKIP_STEM_SUFFIXES = ("_redesign", "_regress", "_sample", "_helper")


def roster(repo_root: Path) -> list[str]:
    """Union of names with a ``data/dcf_assumptions/<T>.json`` and/or a ``dcf/<T>.xlsx``."""
    names: set[str] = set()
    assum = repo_root / "data" / "dcf_assumptions"
    if assum.is_dir():
        names.update(p.stem.upper() for p in assum.glob("*.json"))
    dcfdir = repo_root / "dcf"
    if dcfdir.is_dir():
        for p in dcfdir.glob("*.xlsx"):
            stem = p.stem
            if stem.startswith("_") or any(stem.endswith(s) for s in _SKIP_STEM_SUFFIXES):
                continue
            names.add(stem.upper())
    return sorted(names)


def _build_one(ticker: str, repo_root: Path, tmp_dir: Path, floor: float) -> dict[str, object]:
    """Run the builder for one ticker; parse RESULT (cov=, seg) or SKIP reason."""
    env = dict(
        os.environ,
        DCF_TICKER=ticker,
        DCF_REPO_ROOT=str(repo_root),
        DCF_DEST=str(tmp_dir / f"{ticker}.xlsx"),
        DCF_COVERAGE_FLOOR=str(floor),
    )
    try:
        proc = subprocess.run(
            [*managed_python_prefix(PROJECT_ROOT), str(BUILDER)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ticker": ticker, "status": "failed", "reason": "timeout"}

    lines = proc.stdout.splitlines()
    result = next((ln for ln in lines if ln.startswith("RESULT")), None)
    skip = next((ln for ln in lines if ln.startswith("SKIP")), None)
    cov_line = next((ln for ln in proc.stderr.splitlines() if ln.startswith("COVERAGE")), None)

    if result is not None:
        fields = result.split("\t")
        seg = fields[5] if len(fields) > 5 else "?"
        cov_field = next((f for f in fields if f.startswith("cov=")), "cov=?")
        cov_str = cov_field[len("cov=") :]
        try:
            cov = None if cov_str == "n/a" else float(cov_str)
        except ValueError:
            cov = None
        value = None
        with contextlib.suppress(IndexError, ValueError):
            value = float(fields[2])
        return {
            "ticker": ticker,
            "status": "built",
            "seg": seg,
            "coverage": cov,
            "value": value,
            "coverage_forced": cov_line is not None,
        }
    if skip is not None:
        parts = skip.split("\t")
        return {
            "ticker": ticker,
            "status": "skipped",
            "reason": parts[2] if len(parts) > 2 else "skip",
        }
    tail = (proc.stderr.strip().splitlines() or [""])[-1][:120]
    return {"ticker": ticker, "status": "failed", "reason": tail or "no RESULT/SKIP"}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo-root", required=True, help="Data repo holding data/ and dcf/")
    ap.add_argument(
        "--floor",
        type=float,
        default=COVERAGE_FLOOR,
        help="Coverage floor (default from segment_coverage)",
    )
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", nargs="*", help="Restrict to these tickers")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    floor = float(args.floor)
    names: list[str] = [str(t).upper() for t in args.only] if args.only else roster(repo_root)
    print(f"Auditing {len(names)} names against coverage floor {floor:.2f} (repo {repo_root})\n")

    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)

        def _run(ticker: str) -> dict[str, object]:
            return _build_one(ticker, repo_root, tmp_dir, floor)

        with ThreadPoolExecutor(max_workers=int(args.workers)) as ex:
            results = list(ex.map(_run, names))

    built = [r for r in results if r["status"] == "built"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]

    def _coverage_of(r: dict[str, object]) -> float | None:
        c = r.get("coverage")
        return c if isinstance(c, (int, float)) else None

    def _cov_key(r: dict[str, object]) -> float:
        c = _coverage_of(r)
        return c if c is not None else 99.0

    below = sorted(
        (r for r in built if (c := _coverage_of(r)) is not None and c < floor), key=_cov_key
    )
    ok = sorted(
        (r for r in built if not ((c := _coverage_of(r)) is not None and c < floor)), key=_cov_key
    )

    print(
        f"=== BELOW FLOOR (coverage < {floor:.2f}) -> guard forces whole-company: {len(below)} ==="
    )
    for r in below:
        c = r["coverage"]
        cov_s = f"{c:.2f}" if isinstance(c, (int, float)) else "n/a"
        flag = " [coverage-forced]" if r.get("coverage_forced") else ""
        print(f"  {r['ticker']:6s} cov={cov_s}  seg={r['seg']:7s} value={r['value']}{flag}")

    print(f"\n=== AT/ABOVE FLOOR (per-segment build stands): {len(ok)} ===")
    for r in ok:
        c = r["coverage"]
        cov_s = f"{c:.2f}" if isinstance(c, (int, float)) else "n/a"
        print(f"  {r['ticker']:6s} cov={cov_s}  seg={r['seg']}")

    if skipped:
        print(
            f"\n=== SKIPPED (not an FCFF per-segment DCF / insufficient data): {len(skipped)} ==="
        )
        for r in sorted(skipped, key=lambda r: str(r["ticker"])):
            print(f"  {r['ticker']:6s} {r['reason']}")
    if failed:
        print(f"\n=== FAILED: {len(failed)} ===")
        for r in sorted(failed, key=lambda r: str(r["ticker"])):
            print(f"  {r['ticker']:6s} {r['reason']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

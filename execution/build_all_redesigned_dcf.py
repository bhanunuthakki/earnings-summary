"""Fan the redesigned-DCF builder out over a set of tickers.

For each ticker: optionally refresh its Opus assumption pass first (``--opus``),
then build ``<out-dir>/<T>.xlsx`` via ``build_redesigned_dcf.py``. Financials that
Opus flagged ``dcf_applicable=false`` (banks/insurers/asset-managers) print SKIP.
Prints a summary table (value/share, price, upside, segments, status).

``--out-dir`` defaults to the CANONICAL workbook location ``dcf/`` (S11): the
refresher, the Sheets round-trip, the served ``/dcf/<T>`` route and
``dcf_runs.notes`` all resolve ``dcf/<T>.xlsx``. A from-scratch build here is
edit-safe BY DESIGN: every refresh mirrors workbook edits back into
``data/dcf_assumptions/<T>.json`` (creating the file when absent), and this
builder reads that JSON — so rebuilding reproduces the user's current inputs
rather than reverting them. Day-to-day, prefer ``refresh_dcf.py --all-named``
(capture→rebuild→inject, plus dcf_runs persistence); this fan-out is for
seeding new names and format migrations. The old ``dcf/redesign/`` default
left two diverging copies per name — the DCF coverage panel flags any
leftovers there as superseded.

Usage::

    python execution/build_all_redesigned_dcf.py                  # maintained DCF names
    python execution/build_all_redesigned_dcf.py --tickers AMZN GOOG V
    python execution/build_all_redesigned_dcf.py --opus           # refresh Opus first
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
HERE = Path(__file__).resolve().parent

# Resolve the shared universe helper from THIS checkout's src (not DCF_REPO_ROOT),
# so the maintained set is computed from the code we're running while data is read
# from DCF_REPO_ROOT.
sys.path.insert(0, str(HERE.parent / "src"))

from dcf.universe import dcf_universe  # noqa: E402
from runtime.python_process import managed_python_prefix  # noqa: E402


def default_tickers(repo: Path = REPO) -> list[str]:
    """The maintained DCF universe: every briefed-list ticker (portfolio +
    evaluation) from the DB, unioned with any existing ``dcf/<T>.xlsx`` workbook
    (minus helper/sample files). Pulling the briefed lists — not just the names
    that already have a workbook — makes evaluation-list names first-class: they
    get built even before their first workbook exists."""
    out: set[str] = set(dcf_universe(repo))
    for p in sorted((repo / "dcf").glob("*.xlsx")):
        name = p.stem
        if name.startswith("_") or name.endswith("_redesign"):
            continue
        out.add(name)
    return sorted(out)


def _run(script: str, ticker: str, dest: Path | None = None) -> tuple[str, str, int]:
    env = dict(os.environ, DCF_TICKER=ticker, DCF_REPO_ROOT=str(REPO))
    if dest is not None:
        env["DCF_DEST"] = str(dest)
    proc = subprocess.run(
        [*managed_python_prefix(HERE.parent), str(HERE / script)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout, proc.stderr, proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", nargs="*", help="tickers to build (default: maintained DCF set)")
    ap.add_argument("--opus", action="store_true", help="refresh Opus assumptions before building")
    ap.add_argument(
        "--out-dir",
        default=str(REPO / "dcf"),
        help="output dir (default: the canonical dcf/ — see module docstring)",
    )
    args = ap.parse_args()

    tickers = args.tickers or default_tickers()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    built = skipped = failed = 0
    for ticker in tickers:
        if args.opus:
            o_out, _o_err, _rc = _run("dcf_opus_assumptions.py", ticker)
            tail = (o_out.strip().splitlines() or [f"opus {ticker}: no output"])[-1]
            print(f"  opus: {tail}")
        out, err, _rc = _run("build_redesigned_dcf.py", ticker, out_dir / f"{ticker}.xlsx")
        line = next((ln for ln in out.splitlines() if ln.startswith(("RESULT", "SKIP"))), None)
        if line is None:
            failed += 1
            print(f"FAIL\t{ticker}\t{(err.strip().splitlines() or [''])[-1][:90]}")
        else:
            print(line)
            built += line.startswith("RESULT")
            skipped += line.startswith("SKIP")

    print(
        f"\nBuilt {built} workbooks, skipped {skipped} (non-applicable financials), "
        f"{failed} failed. -> {out_dir}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

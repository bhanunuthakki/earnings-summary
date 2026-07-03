"""Populate the per-name DCF scenario prior (LLM-set weights + rationale) into
``data/dcf_assumptions/<T>.json["redesign"]["scenario_prior"]``.

This is the producer for feature 2: for each DCF name it composes the thesis/bear/
priors anchors and runs the governed ``scenario_prior`` LLM purpose
(``dcf.scenario_prior``), then writes the resulting Bull/Base/Bear weights +
rationale into the assumptions JSON — the from-scratch-build default the redesigned
workbook seeds its yellow weight cells from. From there ``refresh_dcf`` mirrors the
(owner-editable) workbook cells back into this block, and the owner's edit always
wins (``set_by`` flips to ``"owner"``).

Discipline:
- **Owner wins**: a block already marked ``set_by == "owner"`` is never overwritten.
- **Only real priors are written**: a name with no thesis/bear anchors yields the
  global prior (no LLM spend) and is skipped — the JSON stays clean and the workbook
  seeds the symmetric default.
- **A redesign block must already exist** (Opus pass or a prior sync); the producer
  never fabricates one.
- Dry run by default; ``--apply`` writes. ``data/dcf_assumptions`` is gitignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import universe as universe_mod  # noqa: E402
from dcf.scenario_prior import ScenarioPrior, set_scenario_prior_for_ticker  # noqa: E402


def write_scenario_prior_to_json(
    path: Path, prior: ScenarioPrior, *, respect_owner: bool = True
) -> str:
    """Write ``prior`` into ``path``'s ``redesign.scenario_prior`` sub-block,
    preserving every other key. Returns a status:

    * ``"written"`` — the block was updated with the LLM weights + rationale;
    * ``"skipped_owner"`` — an owner-set prior is already on file (owner wins);
    * ``"skipped_global"`` — the prior is the global fallback (no per-name call);
    * ``"skipped_no_redesign_block"`` — no redesign block to annotate (never fabricated);
    * ``"failed_read"`` / ``"failed_write"`` — I/O problem (surfaced, never silent).
    """
    if prior.set_by == "global":
        return "skipped_global"
    if not path.exists():
        return "skipped_no_redesign_block"
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "failed_read"
    if not isinstance(raw, dict):
        return "failed_read"
    data = cast("dict[str, object]", raw)
    block = data.get("redesign")
    if not isinstance(block, dict):
        return "skipped_no_redesign_block"
    rd = cast("dict[str, object]", block)
    existing = rd.get("scenario_prior")
    if (
        respect_owner
        and isinstance(existing, dict)
        and cast("dict[str, object]", existing).get("set_by") == "owner"
    ):
        return "skipped_owner"
    rd["scenario_prior"] = {
        "bull_weight": prior.bull,
        "base_weight": prior.base,
        "bear_weight": prior.bear,
        "rationale": prior.rationale,
        "set_by": prior.set_by,
        "as_of": prior.as_of,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return "failed_write"
    return "written"


def set_priors(
    repo_root: Path, tickers: list[str], *, apply: bool, respect_owner: bool = True
) -> dict[str, str]:
    """Run the scenario_prior LLM over each ticker and (when ``apply``) persist it.
    Returns ticker -> status. A per-ticker LLM error degrades to the global prior
    (``dcf.scenario_prior.generate_scenario_prior``), surfaced as ``skipped_global``."""
    out: dict[str, str] = {}
    for ticker in tickers:
        t = ticker.upper()
        prior = set_scenario_prior_for_ticker(t, repo_root)
        path = repo_root / "data" / "dcf_assumptions" / f"{t}.json"
        if apply:
            status = write_scenario_prior_to_json(path, prior, respect_owner=respect_owner)
        else:
            # Dry run: compute + report what WOULD be written (the LLM has run).
            if prior.set_by == "global":
                status = "would_skip_global"
            elif not path.exists() or not _has_redesign_block(path):
                status = "would_skip_no_redesign_block"
            else:
                status = "would_write"
        weights = f"{prior.bull:.2f}/{prior.base:.2f}/{prior.bear:.2f}"
        print(f"  {t:6s} {status:26s} weights(bull/base/bear)={weights} set_by={prior.set_by}")
        out[t] = status
    return out


def _has_redesign_block(path: Path) -> bool:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(raw, dict) and isinstance(
        cast("dict[str, object]", raw).get("redesign"), dict
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="repo root (holds data/dcf_assumptions)")
    parser.add_argument("--ticker", action="append", help="ticker(s); default = the DCF universe")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument(
        "--overwrite-owner",
        action="store_true",
        help="also overwrite owner-set priors (default: owner wins)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    tickers = (
        [t.upper() for t in args.ticker] if args.ticker else universe_mod.dcf_universe(repo_root)
    )
    if not tickers:
        print("No tickers to process.", file=sys.stderr)
        return 1

    mode = "APPLY" if args.apply else "DRY RUN (pass --apply to write)"
    print(f"Setting scenario priors — {mode}\n  repo_root={repo_root}\n  {len(tickers)} name(s)")
    counts = set_priors(
        repo_root, tickers, apply=args.apply, respect_owner=not args.overwrite_owner
    )
    tally: dict[str, int] = {}
    for status in counts.values():
        tally[status] = tally.get(status, 0) + 1
    print("\nSummary: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""execution/export_book_cma.py — the wealthplan hand-off export (PR4,
directives/monthly_red_team.md Phase 3).

Runs the fat-tailed book Monte Carlo (``portfolio_montecarlo``) off the local
price cache + materialized weights cache, plus the joint-LatAm event stress,
and writes ``data/dashboard/book_cma.json`` (``models.book_cma.BookCmaExport``)
— realized book vol (cov-based + simulated), tail stats under both the normal
and Student-t models, coverage, and provenance. This is the ONLY artifact the
wealthplan project reads from this repo; this script never writes anywhere
outside earnings-summary's own ``data/`` tree.

Idempotent: the ``weights_as_of`` stamp (the materialized weights cache's
``computed_at``) is the idempotency key — a re-run with an unchanged weights
snapshot is a no-op logged as "already done" unless ``--force`` is passed
(directive: "before executing, check whether the deliverable for that key
already exists"). A missing weights cache or too-thin price history halts
loud (exit 1) rather than writing a garbage export.

CLI:
    python execution/export_book_cma.py
    python execution/export_book_cma.py --force
    python execution/export_book_cma.py --n-paths 20000 --seed 7 --out .tmp/book_cma.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.book_cma import (  # noqa: E402
    BookCmaExport,
    BookVol,
    EventStressExport,
    EventStressLeg,
    TailStats,
)
from portfolio_montecarlo import (  # noqa: E402
    DEFAULT_N_PATHS,
    DEFAULT_SEED,
    DEFAULT_T_DF,
    WEALTHPLAN_CMA_ASSUMED_VOL_PCT,
    DistributionRead,
    MonteCarloRead,
    build_book_monte_carlo,
    build_joint_latam_stress,
)
from portfolio_weights import (  # noqa: E402
    read_materialized_weights,
    read_materialized_weights_as_of,
)


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _default_out(repo_root: Path) -> Path:
    return repo_root / "data" / "dashboard" / "book_cma.json"


def _tail_stats(d: DistributionRead) -> TailStats:
    return TailStats(
        method=d.method,
        mean_pct=d.mean_pct,
        vol_pct=d.vol_pct,
        pct_5th=d.pct_5th,
        pct_1st=d.pct_1st,
        prob_below=d.prob_below,
    )


def _atomic_write_json(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="book_cma.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def build_export(
    repo_root: Path,
    db_path: Path,
    *,
    n_paths: int = DEFAULT_N_PATHS,
    seed: int = DEFAULT_SEED,
    t_df: int = DEFAULT_T_DF,
) -> tuple[BookCmaExport | None, str]:
    """Assemble the export payload. Returns ``(None, reason)`` when there's
    nothing exportable (no weights cache, or too little history to simulate)
    so the CLI can halt loud with a specific reason rather than a bare
    non-zero exit."""
    weights = read_materialized_weights(repo_root)
    if not weights:
        return None, (
            "no materialized weights cache on file (run the morning pipeline's "
            "stage 0c reconciliation, or start the tracker once to seed it)"
        )
    weights_as_of = read_materialized_weights_as_of(repo_root)

    mc: MonteCarloRead | None = build_book_monte_carlo(
        repo_root, weights, n_paths=n_paths, seed=seed, t_df=t_df
    )
    if mc is None:
        return None, "not enough daily price history across two or more modeled holdings"

    latam = build_joint_latam_stress(db_path, weights) if db_path.exists() else None
    latam_export = (
        EventStressExport(
            scenario_id=latam.scenario_id,
            title=latam.title,
            book_return_pct=latam.book_return_pct,
            legs=[
                EventStressLeg(
                    ticker=leg.ticker,
                    weight_pct=leg.weight_pct,
                    return_pct=leg.return_pct,
                    label=leg.label,
                )
                for leg in latam.legs
            ],
            notes=latam.notes,
        )
        if latam is not None
        else None
    )

    payload = BookCmaExport(
        computed_at=datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        weights_as_of=weights_as_of,
        tickers_modeled=mc.tickers,
        n_tickers_modeled=len(mc.tickers),
        modeled_weight_pct=mc.modeled_weight_pct,
        unmodeled_weight_pct=mc.unmodeled_weight_pct,
        dropped=mc.dropped,
        wealthplan_cma_assumed_vol_pct=WEALTHPLAN_CMA_ASSUMED_VOL_PCT,
        book_vol=BookVol(cov_based_pct=mc.analytic_vol_pct, simulated_normal_pct=mc.normal.vol_pct),
        normal_tail=_tail_stats(mc.normal),
        student_t_tail=_tail_stats(mc.student_t),
        t_df=mc.t_df,
        n_paths=mc.n_paths,
        seed=mc.seed,
        drift_source=mc.drift_source,
        joint_latam_stress=latam_export,
    )
    return payload, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--db-path", type=Path, default=None, help="Defaults to <repo-root>/data/portfolio.db."
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="Defaults to data/dashboard/book_cma.json."
    )
    parser.add_argument("--n-paths", type=int, default=DEFAULT_N_PATHS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--t-df", type=int, default=DEFAULT_T_DF)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute even when weights_as_of matches the last export (idempotency override).",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )
    out_path: Path = args.out if args.out is not None else _default_out(repo_root)

    weights_as_of = read_materialized_weights_as_of(repo_root)
    if not args.force and out_path.exists() and weights_as_of is not None:
        try:
            existing_raw: object = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_raw = None
        if isinstance(existing_raw, dict) and existing_raw.get("weights_as_of") == weights_as_of:
            _log("already_done", weights_as_of=weights_as_of, out=str(out_path))
            print(json.dumps({"event": "already_done", "weights_as_of": weights_as_of}))
            return 0

    payload, reason = build_export(
        repo_root, db_path, n_paths=args.n_paths, seed=args.seed, t_df=args.t_df
    )
    if payload is None:
        _log("halt", reason=reason)
        print(json.dumps({"event": "halt", "reason": reason}), file=sys.stderr)
        return 1

    _atomic_write_json(out_path, payload.model_dump_json(indent=2))
    summary = {
        "out": str(out_path),
        "n_tickers_modeled": payload.n_tickers_modeled,
        "modeled_weight_pct": round(payload.modeled_weight_pct, 1),
        "book_vol_simulated_pct": round(payload.book_vol.simulated_normal_pct, 1),
        "book_vol_cov_based_pct": round(payload.book_vol.cov_based_pct, 1),
        "t_dist_pct_1st": round(payload.student_t_tail.pct_1st, 1),
    }
    _log("export_done", **summary)
    print(f"Wrote {out_path}")
    print(json.dumps({"event": "export_done", **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

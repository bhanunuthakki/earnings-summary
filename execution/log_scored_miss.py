"""Record a scored miss into the calibration ledger (monthly_red_team.md Phase 3, PR7).

The scored-miss GATE (``src/thesis_reunderwrite_gate.py``, wired into
``compute.thesis_evaluator.persist_verdict``) blocks re-underwriting a thesis
that is currently ``warn``/``breach`` until a Brier-scorable calibration entry
exists for the belief that broke. This CLI is how that entry gets written —
into the EXISTING ``decisions`` calibration ledger (0046/0086/0114/0130), never
a parallel store: a scored miss is one more graded ``decisions`` row, so it
rides the same Brier machinery (``decision_calibration.build_calibration``,
``CONVICTION_PROBABILITY``) as every other graded call, with no new schema.

  --conviction   the Brier-relevant field: the belief's stated conviction
                 (high/medium/low), which is what
                 ``decision_calibration.CONVICTION_PROBABILITY`` maps to an
                 implied probability (0.75/0.55/0.40) for scoring. Required —
                 this is the actual number the compounding loop reads.
  --prior-probability
                 optional free-form documentation of the more precise belief
                 the owner actually held (e.g. "thought ~70% odds the pricing
                 reform stayed contained to one market"). Folded into
                 ``rationale_excerpt`` alongside --belief; NOT a new column —
                 the schema's Brier scoring only understands the conviction
                 bucket, so a --prior-probability that disagrees with
                 --conviction is a documentation mismatch worth fixing, not a
                 separate signal the ledger can use.
  --belief       what was believed at the time (free text) -> rationale_excerpt.
  --outcome      what happened (free text) -> outcome_notes.
  --outcome-label
                 correct|wrong|mixed (default: wrong — a "scored miss" is, by
                 definition, the belief that broke; override only if the
                 grading is genuinely mixed).
  --outcome-pct  optional price move associated with the miss.
  --made-at      when the ORIGINAL belief was held (default: the breach onset
                 read from thesis_evaluations, else today).

Idempotency: keyed on (ticker, created_at-after-onset) the same way the gate
checks it — re-running for a ticker that already has a qualifying scored_miss
row is a no-op unless --force (each scored miss is a distinct judgement at a
distinct time, mirroring pass_decisions.record_pass_decision's manual-pass
convention: always insert unless explicitly told this is a duplicate check).

NVO backfill note (documented, NOT executed here): the directive names NVO's
GLP-1 / US-pricing thesis break as the canonical unscored re-underwrite this
gate would have caught. That backfill — the actual belief text, conviction,
and outcome for NVO's break — is owner-authored history this script does not
fabricate; run this CLI by hand once the owner supplies it:
    python execution/log_scored_miss.py --ticker NVO --conviction <...> \\
        --belief "<what was believed pre-break>" \\
        --outcome "<US pricing reform hit GLP-1 economics>" --made-at <...>

Usage:
    python execution/log_scored_miss.py --ticker NVO --conviction high \\
        --belief "GLP-1 volume growth offsets US price erosion" \\
        --outcome "US pricing reform cut realized price faster than volume grew"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from clock import now_iso  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from thesis_reunderwrite_gate import (  # noqa: E402
    RECOMMENDATION_KIND_SCORED_MISS,
    breach_onset,
    has_scored_miss_since,
)

_CONVICTIONS = ("high", "medium", "low")
_OUTCOME_LABELS = ("correct", "wrong", "mixed")


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}, default=str), file=sys.stderr)


def log_scored_miss(
    *,
    db_path: Path,
    ticker: str,
    conviction: str,
    belief: str,
    outcome: str,
    outcome_label: str = "wrong",
    outcome_pct: float | None = None,
    prior_probability: float | None = None,
    made_at: str | None = None,
    force: bool = False,
) -> tuple[int | None, bool]:
    """Insert one scored-miss ``decisions`` row. Returns ``(decision_id,
    created)`` — ``created=False`` when a qualifying row already existed and
    ``force`` was not passed (``decision_id`` is then that existing row's id,
    or ``None`` if the existing row's id could not be resolved — callers only
    need ``created`` in that path)."""
    symbol = ticker.strip().upper()
    conn = open_db(db_path)
    try:
        onset = breach_onset(conn, symbol)
        if not force and has_scored_miss_since(conn, symbol, onset):
            existing = conn.execute(
                "SELECT id FROM decisions WHERE UPPER(ticker) = ? "
                "AND recommendation_kind = ? ORDER BY created_at DESC LIMIT 1",
                (symbol, RECOMMENDATION_KIND_SCORED_MISS),
            ).fetchone()
            return (int(existing["id"]) if existing is not None else None), False

        rationale = belief.strip()
        if prior_probability is not None:
            rationale += f" [prior probability documented: {prior_probability:.2f}]"
        made_at_x = made_at or onset or now_iso()
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO decisions ("
            "  ticker, recommendation_kind, conviction, decided_by, scope,"
            "  rationale_excerpt, made_at, outcome_at, outcome_label, outcome_pct,"
            "  outcome_notes, created_at"
            ") VALUES (?, ?, ?, 'owner', 'ticker', ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                RECOMMENDATION_KIND_SCORED_MISS,
                conviction,
                rationale[:512],
                made_at_x,
                now,
                outcome_label,
                outcome_pct,
                outcome.strip()[:4000],
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0), True
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--conviction", required=True, choices=_CONVICTIONS)
    parser.add_argument("--belief", required=True, help="What was believed (free text).")
    parser.add_argument("--outcome", required=True, help="What happened (free text).")
    parser.add_argument("--outcome-label", default="wrong", choices=_OUTCOME_LABELS)
    parser.add_argument("--outcome-pct", type=float, default=None)
    parser.add_argument(
        "--prior-probability",
        type=float,
        default=None,
        help="0.0-1.0, documentation only — see module docstring.",
    )
    parser.add_argument(
        "--made-at",
        default=None,
        help="ISO stamp for when the ORIGINAL belief was held (default: breach onset, else now).",
    )
    parser.add_argument(
        "--force", action="store_true", help="Insert even if a qualifying row already exists."
    )
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "portfolio.db")
    args = parser.parse_args(argv)

    if args.prior_probability is not None and not (0.0 <= args.prior_probability <= 1.0):
        print(json.dumps({"error": "--prior-probability must be within [0.0, 1.0]"}))
        return 1

    decision_id, created = log_scored_miss(
        db_path=args.db.resolve(),
        ticker=args.ticker,
        conviction=args.conviction,
        belief=args.belief,
        outcome=args.outcome,
        outcome_label=args.outcome_label,
        outcome_pct=args.outcome_pct,
        prior_probability=args.prior_probability,
        made_at=args.made_at,
        force=args.force,
    )
    _log(
        "scored_miss_logged",
        ticker=args.ticker.upper(),
        decision_id=decision_id,
        created=created,
    )
    print(
        json.dumps(
            {"ticker": args.ticker.upper(), "decision_id": decision_id, "created": created},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

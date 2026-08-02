"""Bear-realism lint (Monthly Red Team Phase 1, guard 2).

The 2026-07 adversarial review found ``BEAR_SEED``'s generic -3pt growth /
-1pt margin / -2x exit-multiple offsets produce bear cases that sit AT or
ABOVE the live price for several portfolio names (UBER, WIX, NVO, BKNG at the
time of the review) — a "bear" that isn't downside at all. Every scenario-
reward consumer (``dcf.scenario_reward``, the next-dollar model, the L7
risk-parity gap, ``portfolio_tail_stress``) reads those names as having no
modeled downside, which is silently worse than having no bear case on file.

This module is the read-only classifier: for every held ticker, load the
latest TOP-LEVEL (unsegmented, ``is_latest``) ``dcf_runs`` row and classify its
bear leg —

* ``missing``     — no top-level run, or the run has no persisted bear scenario
* ``not_a_bear``  — the bear fair value is AT or ABOVE the live price
* ``shallow``     — the bear case is below live price but by less than
                     :data:`SHALLOW_BEAR_FLOOR_PCT` — a mild-disappointment
                     bear, not a thesis-break
* ``ok``          — a real, materially-below-price bear case

Bear provenance (``dcf.scenario_reward.parse_scenario_bear_provenance`` —
``"seed" | "thesis" | "owner"``, Phase 1 guard 3) rides along wherever it is
on file, so a ``seed``-provenance bear on a portfolio name reads as a lint
finding even when it happens to clear the 15% floor: the generic offset
producing a plausible-looking number by coincidence is still not a thesis
read.

Pure disk/DB reads (no network, no LLM) — renders with the tracker down,
mirrors ``portfolio_tail_stress``'s degrade discipline (``{}``/``None`` on a
missing DB rather than raising). Dropped/excluded names are always listed
with a reason, never silently absent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dcf.latest import latest_dcf_rows_from_db
from dcf.scenario_reward import parse_scenario_bear_provenance, parse_scenario_fair_values
from portfolio_weights import read_materialized_weights

__all__ = [
    "SHALLOW_BEAR_FLOOR_PCT",
    "STATUS_MISSING",
    "STATUS_NOT_A_BEAR",
    "STATUS_OK",
    "STATUS_SHALLOW",
    "BearLintFinding",
    "BearLintReport",
    "build_bear_lint",
]

STATUS_MISSING = "missing"
STATUS_NOT_A_BEAR = "not_a_bear"
STATUS_SHALLOW = "shallow"
STATUS_OK = "ok"

# A bear case less than 15% below the live price reads as ordinary volatility,
# not a thesis-break bear — BEAR_SEED's generic offsets rarely clear this bar
# for a name whose thesis actually depends on a specific, severe break.
SHALLOW_BEAR_FLOOR_PCT = 15.0

_STATUS_PRIORITY: dict[str, int] = {
    STATUS_NOT_A_BEAR: 0,
    STATUS_MISSING: 1,
    STATUS_SHALLOW: 2,
    STATUS_OK: 3,
}


@dataclass(slots=True, frozen=True)
class BearLintFinding:
    """One held name's bear-realism read."""

    ticker: str
    status: str  # "missing" | "not_a_bear" | "shallow" | "ok"
    weight_pct: float
    live_price: float | None
    bear_fv: float | None
    bear_return_pct: float | None  # bear_fv/live_price - 1, in percent
    provenance: str | None  # "seed" | "thesis" | "owner" | None (no bear leg on file)
    reason: str


@dataclass(slots=True, frozen=True)
class BearLintReport:
    """The book-wide bear-realism lint."""

    findings: list[BearLintFinding] = field(default_factory=list[BearLintFinding])

    @property
    def flagged(self) -> list[BearLintFinding]:
        """Findings that are NOT a clean ``ok`` — the attention list."""
        return [f for f in self.findings if f.status != STATUS_OK]


@dataclass(slots=True)
class _Row:
    live_price: float | None
    bear_fv: float | None
    provenance: str | None
    has_bear_leg: bool


def _latest_top_level_rows(db_path: Path, tickers: Sequence[str]) -> dict[str, _Row]:
    """Latest TOP-LEVEL (``is_latest``, unsegmented) ``dcf_runs`` row per name —
    ``{}`` on a missing DB / table. Reads through the canonical shared reader
    (``dcf.latest.latest_dcf_rows_from_db``, PR: canonical latest_dcf_run
    reader) rather than a locally hand-rolled query."""
    want = {t.upper() for t in tickers}
    out: dict[str, _Row] = {}
    for t, row in latest_dcf_rows_from_db(db_path).items():
        if t not in want:
            continue
        snapshot = row.assumption_snapshot_json
        fair_values = parse_scenario_fair_values(snapshot)
        out[t] = _Row(
            live_price=row.live_price,
            bear_fv=fair_values.get("bear"),
            provenance=parse_scenario_bear_provenance(snapshot),
            has_bear_leg="bear" in fair_values,
        )
    return out


def _classify(t: str, w_pct: float, row: _Row | None) -> BearLintFinding:
    if row is None:
        return BearLintFinding(
            ticker=t,
            status=STATUS_MISSING,
            weight_pct=w_pct,
            live_price=None,
            bear_fv=None,
            bear_return_pct=None,
            provenance=None,
            reason="no top-level DCF run on file",
        )
    if not row.has_bear_leg or row.bear_fv is None:
        return BearLintFinding(
            ticker=t,
            status=STATUS_MISSING,
            weight_pct=w_pct,
            live_price=row.live_price,
            bear_fv=None,
            bear_return_pct=None,
            provenance=row.provenance,
            reason="no bear scenario persisted",
        )
    if row.live_price is None or row.live_price <= 0:
        return BearLintFinding(
            ticker=t,
            status=STATUS_MISSING,
            weight_pct=w_pct,
            live_price=row.live_price,
            bear_fv=row.bear_fv,
            bear_return_pct=None,
            provenance=row.provenance,
            reason="DCF has no usable live price",
        )
    bear_ret_pct = (row.bear_fv / row.live_price - 1.0) * 100.0
    if row.bear_fv >= row.live_price:
        status = STATUS_NOT_A_BEAR
        reason = (
            f"bear fair value ${row.bear_fv:,.2f} sits AT/ABOVE live price "
            f"${row.live_price:,.2f} — not downside"
        )
    elif -bear_ret_pct < SHALLOW_BEAR_FLOOR_PCT:
        status = STATUS_SHALLOW
        reason = (
            f"bear case only {bear_ret_pct:+.0f}% below live — under the "
            f"{SHALLOW_BEAR_FLOOR_PCT:.0f}% floor for a thesis-break bear"
        )
    else:
        status = STATUS_OK
        reason = f"bear case {bear_ret_pct:+.0f}% below live"
    return BearLintFinding(
        ticker=t,
        status=status,
        weight_pct=w_pct,
        live_price=row.live_price,
        bear_fv=row.bear_fv,
        bear_return_pct=bear_ret_pct,
        provenance=row.provenance,
        reason=reason,
    )


def build_bear_lint(db_path: Path, repo_root: Path | None = None) -> BearLintReport:
    """The book-wide bear-realism lint over every held ticker (materialized
    weights cache — the same substrate ``portfolio_tail_stress`` degrades to
    when the tracker is offline). ``repo_root`` defaults to ``db_path``'s
    grandparent (the repo root holding ``data/portfolio.db``)."""
    weights = read_materialized_weights(repo_root or db_path.parent.parent)
    positive = {t.upper(): w for t, w in weights.items() if w > 0}
    if not positive:
        return BearLintReport(findings=[])
    rows = _latest_top_level_rows(db_path, list(positive))
    findings = [_classify(t, positive[t] * 100.0, rows.get(t)) for t in sorted(positive)]
    findings.sort(key=lambda f: (_STATUS_PRIORITY[f.status], f.ticker))
    return BearLintReport(findings=findings)

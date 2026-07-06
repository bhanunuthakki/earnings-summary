"""Split-basis normalization + guard for FMP analyst-estimates per-share series.

Problem this solves
-------------------
FMP's `analyst-estimates` endpoint splices two share-count bases into one
per-share series for a name that has split. Confirmed on BKNG (post its 2024
split): the historical actual rows carry the OLD pre-split per-share basis
(2019 epsAvg=101.53), while the 2024-onward forward rows carry the split-adjusted
basis (2024 epsAvg=7.31 — a ~20x step at the 2023->2024 boundary that is the
split factor, NOT an earnings collapse). The aggregate `netIncomeAvg` is *also*
corrupted in the historical rows (BKNG 2023 netIncomeAvg=1.187e11 = $118.7B vs a
real ~$4.3B). `revenueAvg` crosses the boundary cleanly — contamination is
isolated to the PER-SHARE series (eps*) and the derived `netIncome*` aggregates.

Why the income statement is the authority
----------------------------------------
FMP delivers `income_statement_annual.json` fully split-adjusted onto the CURRENT
share basis for the entire history (BKNG 2019 income eps=4.52 sits on the same
~840M-share basis as the 2024 forward estimates). That file is already cached for
every ticker, so this module needs NO new network call: it derives the current
basis and the historical mis-basis factor from data already on disk.

Two responsibilities (one deep module, small interface)
------------------------------------------------------
1. `normalize_estimates(...)` — force every per-share row onto ONE current basis.
   The forward rows (which the prod render path and the DCF actually consume) are
   the current basis by construction; historical rows on a stale pre-split basis
   are rescaled onto it. Also repairs a grossly-corrupted `netIncomeAvg` where it
   is implausible against `epsAvg * current_shares`. Idempotent: re-running on an
   already-normalized series is a no-op.
2. `detect_split_discontinuity(...)` — a pure guard that flags a per-share YoY
   step exceeding a split-plausible threshold with no reconciling authority, so
   the ingest path can quarantine (dump raw to .tmp/) instead of silently caching
   a spliced series.

Both functions are pure (no I/O) and typed; the ingest wiring lives in
`execution/save_fmp_data.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Per-share fields in an FMP analyst-estimates row that live on the SPLIT basis.
# Rescaling these by the split factor moves a row from an old basis onto the
# current one. `revenueAvg`, `ebitda*`, `ebit*`, `sgaExpense*` are dollar
# aggregates that DON'T move on a split, so they are deliberately excluded.
PER_SHARE_FIELDS: tuple[str, ...] = ("epsAvg", "epsHigh", "epsLow")

# Aggregate net-income fields. These are dollar totals (split-invariant), but FMP
# corrupts them in the same historical rows (BKNG 2023 = $118.7B). They are NOT
# rescaled by the split factor; they are repaired independently against a
# plausibility band (see `_repair_net_income`).
NET_INCOME_FIELDS: tuple[str, ...] = ("netIncomeAvg", "netIncomeHigh", "netIncomeLow")

# A per-share YoY step at least this large (or its reciprocal) with a continuous
# revenue line is not an earnings event — it is a share-count basis change. 2.5x
# is below the smallest common split (3:2 = 1.5x is intentionally NOT flagged; it
# is within the range of a real strong/weak earnings year) yet safely under a 2:1
# split. Chosen so a genuine doubling of EPS from operations does not trip it but
# any real stock split (>=2:1, and the reciprocal for reverse splits) does.
SPLIT_STEP_THRESHOLD: float = 2.5

# Revenue must be roughly continuous across the boundary for a per-share jump to
# read as a split rather than a real inflection (a genuine ~3x EPS swing usually
# rides a large revenue move). Outside this band we do NOT treat the step as a
# split — we leave the row alone and let the guard flag it for inspection.
_REVENUE_CONTINUITY_LO: float = 0.6
_REVENUE_CONTINUITY_HI: float = 1.7

# A `netIncomeAvg` is corrupt if it exceeds `epsAvg * current_shares` by more than
# this multiple (BKNG 2023 overshoots by ~24x). A wide band avoids false repairs
# from ordinary consensus dispersion; only the gross unit/basis artifacts trip it.
_NET_INCOME_IMPLAUSIBLE_MULT: float = 4.0


@dataclass(frozen=True)
class NormalizationEvent:
    """One structured event describing a normalization action, for stderr logs."""

    kind: str  # "rescale_per_share" | "repair_net_income" | "no_authority"
    date: str
    field_name: str
    old_value: float | None
    new_value: float | None
    factor: float | None = None


@dataclass
class NormalizationResult:
    rows: list[dict[str, object]]
    events: list[NormalizationEvent] = field(default_factory=list[NormalizationEvent])
    current_shares: float | None = None
    quarantined: bool = False
    quarantine_reason: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.events)


@dataclass(frozen=True)
class DiscontinuityFinding:
    """A per-share step the guard considers split-plausible."""

    boundary: str  # "YYYY->YYYY"
    field_name: str
    prev_value: float
    curr_value: float
    step: float  # curr / prev
    revenue_step: float | None


def _as_float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _year(row: dict[str, object]) -> int | None:
    d = row.get("date")
    if not isinstance(d, str) or len(d) < 4:
        return None
    try:
        return int(d[:4])
    except ValueError:
        return None


def _current_shares_from_income(
    income_rows: list[dict[str, object]],
) -> float | None:
    """Current diluted share count = the most recent income-statement period's
    weighted-average diluted shares. FMP delivers the income statement fully
    split-adjusted onto the current basis, so its latest share count IS the
    current basis. Falls back to basic shares, then to eps/netIncome implied."""
    dated = sorted(
        (r for r in income_rows if _year(r) is not None),
        key=lambda r: str(r.get("date") or ""),
    )
    for row in reversed(dated):
        for key in ("weightedAverageShsOutDil", "weightedAverageShsOut"):
            sh = _as_float(row.get(key))
            if sh and sh > 0:
                return sh
        eps = _as_float(row.get("eps"))
        ni = _as_float(row.get("netIncome"))
        if eps and ni and eps != 0:
            implied = ni / eps
            if implied > 0:
                return implied
    return None


def _income_eps_by_year(income_rows: list[dict[str, object]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for r in income_rows:
        y = _year(r)
        eps = _as_float(r.get("eps"))
        if y is not None and eps is not None and eps != 0:
            out[y] = eps
    return out


def _basis_factor(
    est_row: dict[str, object],
    income_eps: dict[int, float],
) -> float | None:
    """How many times the estimate row's per-share basis is off the current
    (income-statement) basis, from the SAME fiscal year: factor = estimate_eps /
    income_eps. ~1.0 means the row is already on the current basis; ~20 means the
    estimate row is 20x too large (old, pre-split, more-per-share basis).

    Returns None when the overlap year is missing or either eps is non-positive
    (a growth-off-a-loss year gives a meaningless ratio)."""
    y = _year(est_row)
    if y is None:
        return None
    ie = income_eps.get(y)
    if ie is None or ie <= 0:
        return None
    ee = _as_float(est_row.get("epsAvg"))
    if ee is None or ee <= 0:
        return None
    return ee / ie


def _looks_like_split_factor(factor: float) -> bool:
    """True when `factor` is a clean split ratio (>= the split threshold, or its
    reciprocal for a reverse split) — i.e. large enough to be a basis change, not
    consensus-vs-actual dispersion."""
    return factor >= SPLIT_STEP_THRESHOLD or factor <= (1.0 / SPLIT_STEP_THRESHOLD)


def normalize_estimates(
    estimate_rows: list[dict[str, object]],
    income_rows: list[dict[str, object]],
) -> NormalizationResult:
    """Force every per-share series onto the current split basis using the
    income statement as authority. Returns a NormalizationResult carrying the
    (possibly) rewritten rows + structured events. Non-mutating: input rows are
    deep-copied before edits so callers keep the raw payload for quarantine.

    Behavior:
    - For each estimate row that overlaps an income-statement year, compute the
      basis factor. If it is a clean split ratio, divide the row's per-share
      fields by the factor to move it onto the current basis.
    - Repair a `netIncomeAvg` that is implausible against `epsAvg *
      current_shares`.
    - When there is NO overlap with the income statement at all, we cannot
      establish the current basis -> `quarantined=True` (the ingest path dumps
      raw and skips, per the schema-drift rule) rather than guessing.
    """
    rows: list[dict[str, object]] = [dict(r) for r in estimate_rows]
    events: list[NormalizationEvent] = []

    current_shares = _current_shares_from_income(income_rows)
    income_eps = _income_eps_by_year(income_rows)

    # No authority to normalize against: quarantine instead of guessing. The
    # guard (detect_split_discontinuity) decides whether that quarantine matters.
    if not income_eps and current_shares is None:
        disc = detect_split_discontinuity(estimate_rows)
        if disc:
            return NormalizationResult(
                rows=rows,
                events=[],
                current_shares=None,
                quarantined=True,
                quarantine_reason=(
                    "per-share discontinuity with no income-statement authority "
                    f"to reconcile ({disc[0].boundary} {disc[0].field_name} "
                    f"step={disc[0].step:.2f})"
                ),
            )
        return NormalizationResult(rows=rows, events=[], current_shares=None)

    for row in rows:
        # 1) Rescale a per-share row that sits on a stale (pre-split) basis.
        factor = _basis_factor(row, income_eps)
        if factor is not None and _looks_like_split_factor(factor):
            for fld in PER_SHARE_FIELDS:
                val = _as_float(row.get(fld))
                if val is None:
                    continue
                new_val = val / factor
                row[fld] = new_val
                events.append(
                    NormalizationEvent(
                        kind="rescale_per_share",
                        date=str(row.get("date") or ""),
                        field_name=fld,
                        old_value=val,
                        new_value=new_val,
                        factor=factor,
                    )
                )

        # 2) Repair a grossly-corrupted netIncomeAvg (BKNG 2023 = $118.7B). Use
        # the row's OWN (now-normalized) epsAvg * current_shares as the sanity
        # anchor; only replace when the stored value overshoots the anchor by an
        # implausible multiple, so ordinary dispersion is left untouched.
        _repair_net_income(row, current_shares, events)

    return NormalizationResult(
        rows=rows,
        events=events,
        current_shares=current_shares,
    )


def _repair_net_income(
    row: dict[str, object],
    current_shares: float | None,
    events: list[NormalizationEvent],
) -> None:
    """Replace a `netIncomeAvg`/High/Low that is implausible vs `epsAvg *
    current_shares` with the reconstructed anchor. Mutates `row` + appends
    events. No-op when we lack current shares or a positive epsAvg."""
    if current_shares is None or current_shares <= 0:
        return
    eps = _as_float(row.get("epsAvg"))
    if eps is None or eps <= 0:
        return
    anchor = eps * current_shares  # plausible net income for this (normalized) eps
    for fld in NET_INCOME_FIELDS:
        val = _as_float(row.get(fld))
        if val is None or val <= 0:
            continue
        if val > anchor * _NET_INCOME_IMPLAUSIBLE_MULT:
            row[fld] = anchor
            events.append(
                NormalizationEvent(
                    kind="repair_net_income",
                    date=str(row.get("date") or ""),
                    field_name=fld,
                    old_value=val,
                    new_value=anchor,
                )
            )


def detect_split_discontinuity(
    estimate_rows: list[dict[str, object]],
) -> list[DiscontinuityFinding]:
    """Pure guard: return per-share YoY steps that look like a split basis splice
    (step >= threshold or <= its reciprocal, with revenue roughly continuous).

    This reads the estimates series ALONE — no authority needed — so the ingest
    path can call it to decide whether to quarantine a payload it cannot
    reconcile. An empty result means no split-plausible discontinuity."""
    dated = sorted(
        (r for r in estimate_rows if _year(r) is not None),
        key=lambda r: str(r.get("date") or ""),
    )
    findings: list[DiscontinuityFinding] = []
    prev: dict[str, object] | None = None
    for row in dated:
        if prev is not None:
            findings.extend(_boundary_findings(prev, row))
        prev = row
    return findings


def _boundary_findings(
    prev: dict[str, object],
    curr: dict[str, object],
) -> list[DiscontinuityFinding]:
    """Per-share steps at a single year boundary that read as a split splice."""
    rev0 = _as_float(prev.get("revenueAvg"))
    rev1 = _as_float(curr.get("revenueAvg"))
    rev_step = (rev1 / rev0) if (rev0 and rev1 and rev0 > 0) else None
    # Revenue must be continuous for a per-share jump to read as a split rather
    # than a real inflection. If we can't establish continuity, don't flag.
    if rev_step is None or not (_REVENUE_CONTINUITY_LO < rev_step < _REVENUE_CONTINUITY_HI):
        return []
    out: list[DiscontinuityFinding] = []
    for fld in PER_SHARE_FIELDS:
        v0 = _as_float(prev.get(fld))
        v1 = _as_float(curr.get(fld))
        if v0 is None or v1 is None or v0 <= 0 or v1 <= 0:
            continue
        step = v1 / v0
        if step >= SPLIT_STEP_THRESHOLD or step <= (1.0 / SPLIT_STEP_THRESHOLD):
            out.append(
                DiscontinuityFinding(
                    boundary=f"{_year(prev)}->{_year(curr)}",
                    field_name=fld,
                    prev_value=v0,
                    curr_value=v1,
                    step=step,
                    revenue_step=rev_step,
                )
            )
    return out

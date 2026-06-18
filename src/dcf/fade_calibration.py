"""Per-name calibration of the growth-fade curvature to Street consensus.

The redesigned FCFF engine fades each segment's growth from its near-term rate to
its terminal rate on a convex curve, ``g_t = g_T + (g_1-g_T)·((N-1-j)/(N-1))^p``.
A single global ``p`` (=2.0) is a reasonable default, but the *right* shape is the
one whose revenue path tracks the analyst consensus for that specific name — a
fast decelerator and a durable compounder want different curvatures.

This module solves, per name, for the ``p`` whose projected total revenue best
fits the consensus revenue path over the overlap years, **weighting near-term
years more** (near-term estimates are higher-confidence and their errors compound
into every later year). The endpoints (``g_1``/``g_T``) are held fixed — only the
shape between them is fit — so the calibration never inflates the terminal: ``p``
is floored at 1.0 (a straight-line fade), never the old hold-then-glide that ran
growth hot for a decade.

Used by ``build_redesigned_dcf.py`` (writes the fit ``p`` to the Dashboard) and
mirrored by ``dcf.redesign`` (reads it back). A name with < 2 consensus years, or
an explicit ``growth_fade_curvature`` override in its assumption block, skips the
fit and takes the default / override.
"""

from __future__ import annotations

from collections.abc import Mapping

# Default when there isn't enough consensus to fit (mirrors dcf.redesign).
DEFAULT_CURVATURE = 2.0
# Curvature search range. Floor 1.0 = straight-line fade (never MORE generous than
# linear, so the tail can't re-inflate); ceiling 4.0 = aggressively front-loaded.
CURVATURE_BOUNDS = (1.0, 4.0)
CURVATURE_STEP = 0.05
# Near-term weighting: year-j fit error is scaled by NEAR_WEIGHT_DECAY**j, so the
# first forecast year dominates and year 4+ barely moves the fit (0.6 -> weights
# 1.0, 0.60, 0.36, 0.22, 0.13, ...).
NEAR_WEIGHT_DECAY = 0.6


def project_total_revenue(
    base_by_segment: Mapping[str, float],
    near_growth_by_segment: Mapping[str, float],
    terminal_growth_by_segment: Mapping[str, float],
    curvature: float,
    n_offsets: int,
    n_fc: int,
) -> list[float]:
    """Total revenue for the first ``n_offsets`` forecast years at a given
    ``curvature`` — the convex fade summed across segments, mirroring
    ``dcf.redesign._project`` exactly (year offset ``j`` is 0-indexed; year 0 grows
    at ``near``, year ``n_fc-1`` at ``terminal``)."""
    fade_span = n_fc - 1
    seg = dict(base_by_segment)
    totals: list[float] = []
    for j in range(n_offsets):
        frac = ((fade_span - j) / fade_span) ** curvature if fade_span else 1.0
        for s in seg:
            g1 = near_growth_by_segment[s]
            gt = terminal_growth_by_segment[s]
            seg[s] *= 1.0 + (gt + (g1 - gt) * frac)
        totals.append(sum(seg.values()))
    return totals


def calibrate_curvature(
    base_by_segment: Mapping[str, float],
    near_growth_by_segment: Mapping[str, float],
    terminal_growth_by_segment: Mapping[str, float],
    consensus_by_offset: Mapping[int, float],
    n_fc: int,
    *,
    default: float = DEFAULT_CURVATURE,
    bounds: tuple[float, float] = CURVATURE_BOUNDS,
    step: float = CURVATURE_STEP,
    near_weight_decay: float = NEAR_WEIGHT_DECAY,
) -> float:
    """The fade curvature whose projected revenue best fits consensus.

    ``consensus_by_offset`` maps a 0-indexed forecast-year offset (j=0 is the first
    forecast year) to that year's consensus total revenue ($M). Returns ``default``
    when fewer than 2 positive consensus points are available (nothing to fit).
    Otherwise grid-searches ``[bounds]`` and returns the ``p`` minimising the
    near-term-weighted relative squared error, rounded to 2dp.
    """
    points = [
        (j, c) for j, c in consensus_by_offset.items() if isinstance(c, (int, float)) and c > 0
    ]
    if len(points) < 2:
        return default
    max_offset = max(j for j, _ in points)

    def weighted_error(p: float) -> float:
        proj = project_total_revenue(
            base_by_segment,
            near_growth_by_segment,
            terminal_growth_by_segment,
            p,
            max_offset + 1,
            n_fc,
        )
        err = 0.0
        wsum = 0.0
        for j, cons in points:
            w = near_weight_decay**j
            err += w * ((proj[j] - cons) / cons) ** 2
            wsum += w
        return err / wsum if wsum else float("inf")

    lo, hi = bounds
    n_steps = max(1, round((hi - lo) / step))
    best_p, best_err = default, float("inf")
    for i in range(n_steps + 1):
        p = lo + i * step
        e = weighted_error(p)
        if e < best_err:
            best_err, best_p = e, p
    return round(best_p, 2)

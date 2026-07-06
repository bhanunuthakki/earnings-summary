"""Analyst-defined segment overrides for the redesigned per-segment DCF.

``execution/build_redesigned_dcf.py`` normally pins the modeled segment SET to FMP's
reported product segments (``src/dcf/segment_coverage.py``); the per-name ``_opus
["segments"]`` block can only re-growth those FMP-named segments, never define a NEW
segment with a custom base-revenue split. That is a hard ceiling: an analyst who has
decomposed a company into cleaner economic streams than FMP reports (e.g. WIX into a
declining Creative-Subscriptions core + a fast-scaling Base44 AI-builder engine) had
no way to make that split PERSIST into the model — the narrative carried it, the math
did not.

This module parses + validates an optional ``redesign.analyst_segments`` block:

    "analyst_segments": {
      "Core":   {"base_pct": 0.95, "near_term_growth": 0.12, "terminal_growth": -0.02},
      "Base44": {"base_pct": 0.05, "near_term_growth": 0.40, "terminal_growth":  0.03}
    }

When present and valid, the builder uses these INSTEAD of the FMP-resolved segments:
it splits base-year (income-statement) revenue by ``base_pct``, drives each segment's
growth by its own near/terminal rates, and writes the Dashboard/Financials/Model
segment rows so the pure engine (``src/dcf/redesign.py``) recomputes generically off
them — no engine change needed.

Validation is strict and fails LOUD to the FMP fallback (never a silent
half-applied override): every segment needs a numeric ``base_pct`` in (0, 1] plus
both growth rates, and the ``base_pct`` values must sum to ~1.0. This is the single,
testable decision point so the builder stays a thin call and the contract can be
unit-tested without running the whole top-level build script.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

# base_pct values must sum to within this of 1.0 (rounding / a tiny unallocated
# residual is fine; a real mistake — segments that sum to 0.8 or 1.2 — is not).
_SUM_TOLERANCE = 0.02


def _num(v: object) -> float | None:
    """int/float coercion that rejects bool (a stray True must never be 1.0)."""
    if isinstance(v, bool):
        return None
    return float(v) if isinstance(v, (int, float)) else None


@dataclass(frozen=True)
class AnalystSegment:
    """One analyst-defined revenue stream."""

    name: str
    base_pct: float  # fraction of base-year revenue (0, 1]
    near_term_growth: float
    terminal_growth: float


@dataclass(frozen=True)
class AnalystSegmentSet:
    """The parsed analyst-segment override, or a reason it was rejected.

    ``valid`` gates use: when False the builder ignores ``segments`` and falls back
    to the FMP-resolved set, logging ``reason`` loudly (an override must never be
    silently half-applied). ``names`` preserves the JSON insertion order so the
    Dashboard/Model segment rows render in the order the analyst wrote them.
    """

    valid: bool
    reason: str | None
    segments: list[AnalystSegment] = field(default_factory=list["AnalystSegment"])

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.segments]

    def base_revenue(self, base_fy_revenue: float) -> dict[str, float]:
        """Split a base-year revenue total across the segments by ``base_pct``."""
        return {s.name: s.base_pct * base_fy_revenue for s in self.segments}

    def near_growth(self) -> dict[str, float]:
        return {s.name: s.near_term_growth for s in self.segments}

    def terminal_growth(self) -> dict[str, float]:
        return {s.name: s.terminal_growth for s in self.segments}


def parse_analyst_segments(raw: object) -> AnalystSegmentSet:
    """Parse + validate a ``redesign.analyst_segments`` block.

    Returns an ``AnalystSegmentSet`` with ``valid=False`` and a human-readable
    ``reason`` on any structural problem (not a mapping, empty, a segment missing a
    field, a non-numeric or out-of-range ``base_pct``/growth, or ``base_pct`` values
    that do not sum to ~1.0) so the caller falls back to FMP and logs why. ``valid=
    True`` only when every segment is complete and the split is coherent.
    """
    if raw is None:
        return AnalystSegmentSet(valid=False, reason=None)  # absent block — not an error
    if not isinstance(raw, Mapping) or not raw:
        return AnalystSegmentSet(valid=False, reason="analyst_segments is not a non-empty object")

    segments: list[AnalystSegment] = []
    for name, spec in cast("Mapping[str, object]", raw).items():
        if not isinstance(spec, Mapping):
            return AnalystSegmentSet(valid=False, reason=f"segment {name!r} spec is not an object")
        spec_m = cast("Mapping[str, object]", spec)
        base_pct = _num(spec_m.get("base_pct"))
        near = _num(spec_m.get("near_term_growth"))
        term = _num(spec_m.get("terminal_growth"))
        if base_pct is None:
            return AnalystSegmentSet(
                valid=False, reason=f"segment {name!r} missing numeric base_pct"
            )
        if not (0.0 < base_pct <= 1.0):
            return AnalystSegmentSet(
                valid=False, reason=f"segment {name!r} base_pct {base_pct} not in (0, 1]"
            )
        if near is None or term is None:
            return AnalystSegmentSet(
                valid=False,
                reason=f"segment {name!r} missing numeric near_term_growth/terminal_growth",
            )
        segments.append(
            AnalystSegment(
                name=str(name), base_pct=base_pct, near_term_growth=near, terminal_growth=term
            )
        )

    total = sum(s.base_pct for s in segments)
    if abs(total - 1.0) > _SUM_TOLERANCE:
        return AnalystSegmentSet(
            valid=False, reason=f"base_pct sums to {total:.3f}, not ~1.0 (tol {_SUM_TOLERANCE})"
        )
    return AnalystSegmentSet(valid=True, reason=None, segments=segments)

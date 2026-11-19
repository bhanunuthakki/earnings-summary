"""Assumption records, driver ranking and joint stress for a driver-tree forecast.

The forecast-assumption method
(``src/advisor/skills/earnings-summary-investing/references/forecast-assumptions.md``)
owns the analytical steps. This module makes its record and its arithmetic
executable, so a model cannot report a result while a material assumption has no
anchor, counter-argument, shock range or public observable.

A model builder supplies one pure function ``run(params) -> decision output`` and
one ``DriverAssumption`` per driver. The module then:

* validates every record and fails closed (``DriverModelError``);
* shocks one driver at a time across its recorded range and ranks the drivers;
* moves the material drivers together in each direction (joint stress);
* checks that the driver product reproduces reported history (identity);
* reports why the result is not decision-grade. It never grants that grade.

It is pure standard library. It reads no database, calls no model and writes no
file. To solve for the driver value that a market price implies, pass the same
``run`` to ``dcf.reverse_valuation.solve_monotonic``.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, get_args

AnchorClass = Literal[
    "reported_fact", "management_statement", "calculation", "external_estimate", "judgment"
]
Confidence = Literal["high", "medium", "low"]
Params = dict[str, object]
Change = Callable[[Params], None]
Run = Callable[[Mapping[str, object]], float]

SCHEMA = "driver_model.v1"
_ANCHOR_CLASSES = frozenset(get_args(AnchorClass))
_CONFIDENCES = frozenset(get_args(Confidence))


class DriverModelError(ValueError):
    """A record, shock or model output does not satisfy the method."""


@dataclass(frozen=True, slots=True)
class Anchor:
    """Where a base value comes from. ``history`` holds the reported values beside it."""

    anchor_class: AnchorClass
    source: str
    as_of: str  # ISO date of the source
    locator: str = ""  # page/table/segment, or the formula for a calculation
    history: str = ""
    resolver_admitted: bool = False  # reported fact read through the shared fact resolver
    publisher: str = ""  # external estimate only
    measured: str = ""  # external estimate only: what was measured, and how if known
    error: str = ""  # external estimate only: stated or estimated error


@dataclass(frozen=True, slots=True)
class Shock:
    label: str
    change: Change


@dataclass(frozen=True, slots=True)
class DriverAssumption:
    driver: str
    role: str  # position in the formula
    base_path: str  # value, shape and end state
    anchor: Anchor
    counter_argument: str
    confidence: Confidence
    confidence_reason: str
    observable: str  # source, disclosure and cadence; "none" when no public test exists
    adverse: Shock | None = None
    favourable: Shock | None = None
    shock_reason: str = ""  # why the range is reasonable, or why no shock applies


@dataclass(frozen=True, slots=True)
class DriverEffect:
    driver: str
    adverse_label: str
    adverse_output: float
    favourable_label: str
    favourable_output: float
    material: bool

    def deltas(self, base: float) -> tuple[float, float]:
        return (self.adverse_output - base, self.favourable_output - base)


@dataclass(frozen=True, slots=True)
class DriverRanking:
    base_output: float
    effects: tuple[DriverEffect, ...]  # largest absolute effect first
    joint_adverse_output: float | None  # None when no driver is material
    joint_favourable_output: float | None
    unshocked: tuple[str, ...]  # drivers with a recorded reason for no shock

    @property
    def material_drivers(self) -> tuple[str, ...]:
        return tuple(e.driver for e in self.effects if e.material)


@dataclass(frozen=True, slots=True)
class IdentityPeriod:
    """One reported period: disclosed driver changes and the reported result change."""

    label: str
    driver_changes: tuple[float, ...]  # fractional changes, for example 0.08 for +8%
    reported_change: float


def _is_iso_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def assumption_issues(records: Sequence[DriverAssumption]) -> list[str]:
    """Return every method violation. An empty list means the records are complete."""
    issues: list[str] = []
    seen: set[str] = set()
    if not records:
        return ["no driver assumptions"]
    for r in records:
        name = r.driver.strip() or "<unnamed>"
        if name in seen:
            issues.append(f"{name}: duplicate driver")
        seen.add(name)
        required = {
            "driver": r.driver,
            "role": r.role,
            "base_path": r.base_path,
            "anchor.source": r.anchor.source,
            "counter_argument": r.counter_argument,
            "confidence_reason": r.confidence_reason,
            "observable": r.observable,
            "shock_reason": r.shock_reason,
        }
        issues += [f"{name}: {key} is empty" for key, text in required.items() if not text.strip()]
        a = r.anchor
        if a.anchor_class not in _ANCHOR_CLASSES:
            issues.append(f"{name}: unknown anchor class {a.anchor_class!r}")
        if r.confidence not in _CONFIDENCES:
            issues.append(f"{name}: unknown confidence {r.confidence!r}")
        if not _is_iso_date(a.as_of):
            issues.append(f"{name}: anchor.as_of is not an ISO date")
        if a.anchor_class != "judgment" and not a.locator.strip():
            issues.append(f"{name}: {a.anchor_class} anchor needs a locator or formula")
        if a.anchor_class == "external_estimate":
            missing = [
                key
                for key, text in (
                    ("publisher", a.publisher),
                    ("measured", a.measured),
                    ("error", a.error),
                )
                if not text.strip()
            ]
            if missing:
                issues.append(f"{name}: external estimate needs {', '.join(missing)}")
        if a.resolver_admitted and a.anchor_class != "reported_fact":
            issues.append(f"{name}: only a reported fact can be resolver-admitted")
        if a.anchor_class == "judgment" and r.confidence == "high":
            issues.append(f"{name}: a judgment anchor cannot carry high confidence")
        if (r.adverse is None) != (r.favourable is None):
            issues.append(f"{name}: record both shock directions, or neither with a reason")
        for shock in (r.adverse, r.favourable):
            if shock is not None and not shock.label.strip():
                issues.append(f"{name}: shock label is empty")
    return issues


def _output(run: Run, base: Mapping[str, object], changes: Sequence[Change], what: str) -> float:
    params: Params = copy.deepcopy(dict(base))
    for change in changes:
        change(params)
    value = run(params)
    if not math.isfinite(value):
        raise DriverModelError(f"{what}: model output is not finite")
    return value


def rank_drivers(
    run: Run,
    base: Mapping[str, object],
    records: Sequence[DriverAssumption],
    *,
    material_pct: float = 0.03,
) -> DriverRanking:
    """Shock each driver alone, rank by absolute effect, then stress the material set.

    A driver is material when either shock moves the output by at least
    ``material_pct`` of the base output. The joint stress applies every material
    adverse shock together, then every material favourable shock together. The
    labels follow the output: if an "adverse" shock raises the output, the record
    is wrong and the call fails.
    """
    issues = assumption_issues(records)
    if issues:
        raise DriverModelError("; ".join(issues))
    base_output = _output(run, base, (), "base case")
    if base_output == 0:
        raise DriverModelError("base case output is zero; percent effects are undefined")
    effects: list[DriverEffect] = []
    shocks: dict[str, tuple[Shock, Shock]] = {}
    unshocked: list[str] = []
    for r in records:
        if r.adverse is None or r.favourable is None:
            unshocked.append(r.driver)
            continue
        low = _output(run, base, (r.adverse.change,), f"{r.driver} adverse")
        high = _output(run, base, (r.favourable.change,), f"{r.driver} favourable")
        if low > base_output or high < base_output:
            raise DriverModelError(
                f"{r.driver}: adverse/favourable shocks move the output the wrong way"
            )
        swing = max(abs(low - base_output), abs(high - base_output))
        shocks[r.driver] = (r.adverse, r.favourable)
        effects.append(
            DriverEffect(
                driver=r.driver,
                adverse_label=r.adverse.label,
                adverse_output=low,
                favourable_label=r.favourable.label,
                favourable_output=high,
                material=swing >= material_pct * abs(base_output),
            )
        )
    effects.sort(
        key=lambda e: (
            -max(abs(e.adverse_output - base_output), abs(e.favourable_output - base_output))
        )
    )
    material = [e.driver for e in effects if e.material]
    joint_low = joint_high = None
    if material:
        joint_low = _output(run, base, [shocks[d][0].change for d in material], "joint adverse")
        joint_high = _output(run, base, [shocks[d][1].change for d in material], "joint favourable")
    return DriverRanking(base_output, tuple(effects), joint_low, joint_high, tuple(unshocked))


def identity_gaps(
    periods: Sequence[IdentityPeriod], *, tolerance: float = 0.01
) -> list[tuple[str, float, bool]]:
    """Compare the compounded driver changes with the reported change, per period.

    Returns ``(label, reported minus implied, within tolerance)``. A gap outside
    the tolerance needs an explanation before the tree is used to forecast.
    """
    out: list[tuple[str, float, bool]] = []
    for p in periods:
        implied = math.prod(1.0 + c for c in p.driver_changes) - 1.0
        gap = p.reported_change - implied
        out.append((p.label, gap, abs(gap) <= tolerance))
    return out


def grade_blockers(
    records: Sequence[DriverAssumption],
    ranking: DriverRanking,
    *,
    model_as_of: str,
    latest_reported_period_end: str,
    consensus_fitted: bool = False,
) -> list[str]:
    """Reasons the result is not decision-grade. Empty does not grant the grade."""
    blockers: list[str] = []
    material = set(ranking.material_drivers)
    for r in records:
        a = r.anchor
        if a.anchor_class == "reported_fact" and not a.resolver_admitted:
            blockers.append(f"{r.driver}: reported fact was not read through the fact resolver")
        if r.driver in material and a.anchor_class in ("external_estimate", "judgment"):
            blockers.append(f"{r.driver}: material driver rests on {a.anchor_class}")
        if r.driver in material and r.observable.strip().lower() == "none":
            blockers.append(f"{r.driver}: material driver has no public observable")
    if date.fromisoformat(model_as_of) < date.fromisoformat(latest_reported_period_end):
        blockers.append("model is older than the latest reported period")
    if consensus_fitted:
        blockers.append("growth path is fitted to consensus; this is a consensus-fade estimate")
    return blockers


def _fmt(base: float, value: float, unit: str) -> str:
    return f"{value - base:+,.1f} {unit} ({(value / base - 1) * 100:+.1f}%)"


def render_markdown(
    *,
    question: str,
    conclusion: str,
    output_name: str,
    unit: str,
    records: Sequence[DriverAssumption],
    ranking: DriverRanking,
    blockers: Sequence[str],
    not_modeled: Sequence[str],
    model_as_of: str,
    latest_reported_period: str,
) -> str:
    """Argument first: conclusion, ranked drivers and joint stress; records behind."""
    for label, text in (("question", question), ("conclusion", conclusion)):
        if not text.strip():
            raise DriverModelError(f"{label} is empty")
    if not not_modeled:
        raise DriverModelError("name what is not modeled, or state 'none identified'")
    b = ranking.base_output
    by_name = {r.driver: r for r in records}
    lines = [
        f"**Question:** {question}",
        "",
        conclusion,
        "",
        f"Model as of {model_as_of}; latest reported period {latest_reported_period}. "
        f"Base {output_name}: {b:,.1f} {unit}.",
        "",
        f"## Drivers ranked by effect on {output_name}",
        "",
        "| Driver | Anchor | Confidence | Adverse | Effect | Favourable | Effect |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in ranking.effects:
        r = by_name[e.driver]
        name = e.driver if e.material else f"{e.driver} (minor)"
        lines.append(
            f"| {name} | {r.anchor.anchor_class} | {r.confidence} | {e.adverse_label} | "
            f"{_fmt(b, e.adverse_output, unit)} | {e.favourable_label} | "
            f"{_fmt(b, e.favourable_output, unit)} |"
        )
    lines += ["", "## Joint stress", ""]
    if ranking.joint_adverse_output is None or ranking.joint_favourable_output is None:
        lines.append("No driver is material at the chosen threshold.")
    else:
        names = ", ".join(ranking.material_drivers)
        lines += [
            f"Material drivers moved together ({names}):",
            "",
            f"- All adverse: {_fmt(b, ranking.joint_adverse_output, unit)}",
            f"- All favourable: {_fmt(b, ranking.joint_favourable_output, unit)}",
        ]
    lines += ["", "## Evidence limits", ""]
    lines += [f"- {text}" for text in blockers] or ["- No blocker found by this check."]
    lines += ["", "## Not modeled", ""] + [f"- {text}" for text in not_modeled]
    lines += [
        "",
        "## Assumption records",
        "",
        "| Driver | Role | Base path | Anchor | Counter-argument | Shock range | Observable |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        a = r.anchor
        source = "; ".join(
            part for part in (a.publisher, a.source, a.as_of, a.locator, a.history, a.error) if part
        )
        lines.append(
            f"| {r.driver} | {r.role} | {r.base_path} | {a.anchor_class}: {source} | "
            f"{r.counter_argument} | {r.shock_reason} | {r.observable} |"
        )
    return "\n".join(lines) + "\n"

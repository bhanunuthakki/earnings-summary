"""Inject a picked DIY fact as a DCF *driver* (capture-every-number S6).

The DIY explore panel (``src/pipeline/explore_panel.py``) surfaces every
extracted fact as a pickable token (S5). This module is the bridge that takes
one such token + a target driver field and turns the fact's LATEST stored value
into a yellow-Dashboard assumption on the redesigned FCFF workbook — so a number
the owner found while slicing data can flow straight into the valuation instead
of being hand-retyped (and mis-scaled) into the model.

Three pieces live here, all pure (no ``execution/`` dependency, so the converter
is unit-tested in isolation); the server route (``POST /api/dcf/inject-fact``)
orchestrates them and routes the commit through ``refresh_dcf.apply_edits`` — the
ONLY clobber-safe write path (cell + JSON sync + provenance + dcf_runs):

1. **The field registry** (:data:`DRIVER_FIELDS`) — the fixed set of injectable
   :class:`~dcf.redesign.RedesignInputs` levers, each carrying the unit *family*
   and the sanity *bounds* its target expects. WACC is DELIBERATELY ABSENT: it
   has no input cell (it is derived from rf/ERP/beta/Kd), so a "set WACC" fact
   must be re-expressed as one of those drivers, never written directly.

2. **The units/scale converter** (:func:`convert_to_driver`) — the load-bearing
   correctness piece. Dashboard rates are DECIMAL ratios (0.043, not 4.3),
   capex is $M, multiples are turns. A fact carries its own stored
   :class:`~models.facts.Unit` (``percent`` / ``ratio`` / ``bps`` / ``actual`` /
   ``millions`` / ...); the converter rescales it into the target field's unit
   via the shared :func:`~models.unit_convert.convert_unit`, REFUSING any
   cross-family conversion (a dollar level into a margin) or a unit-less fact
   into a rate field (the 4.3-vs-0.043 trap the program exists to kill). The
   converted value is then bounds-checked — a degenerate/extreme value that
   ``derive_over_under`` would silently map to ``None`` is rejected up front.

3. **Value resolution** (:func:`resolve_fact_value`) — pulls the fact's latest
   value through the SAME ``src/timeseries/loaders`` the ask/engine read path
   uses, so company-doc ``fact_overrides`` precedence is honored (S2). KPI tokens
   are de-fragmented representatives, resolved to this ticker's richest variant
   exactly as ``viewspec.engine`` does; an override-only fact (one FMP never
   carried) is picked up from the override ledger directly.

:func:`apply_to_inputs` produces the edited ``RedesignInputs`` (frozen — a new
instance, never a mutation) the commit path persists.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from compute.kpi_resolver import kpi_group_key
from dcf import redesign as redesign_mod
from models.facts import Unit
from models.unit_convert import convert_unit
from provenance.overrides import (
    DEFAULT_USER_ID,
    FINANCIAL_FACT,
    KPI,
    OverrideAction,
    get_active_overrides,
)
from timeseries.loaders import (
    SourcedObservation,
    load_financial_series_with_provenance,
    load_kpi_series_with_provenance,
    load_segment_junction_series_with_provenance,
)
from viewspec.spec import MetricRef

# Every fiscal_period_type a stored fact might carry — we want the genuinely
# LATEST observation regardless of cadence (a captured annual KPI, a quarterly
# financial, a semi-annual filer's halves), so the resolution query spans them
# all and takes the most-recent period_end. Includes the capture program's
# "annual" alongside the FiscalPeriodType enum values.
_ALL_FACT_PERIOD_TYPES: tuple[str, ...] = (
    "Q1",
    "Q2",
    "Q3",
    "Q4",
    "H1",
    "H2",
    "FY",
    "TTM",
    "annual",
)

# Field families — what unit the target Dashboard cell speaks.
PROPORTION = "proportion"  # a decimal ratio (margin / rate / multiple-of): target Unit.RATIO
MONEY = "money"  # a dollar amount in millions: target Unit.MILLIONS
RAW = "raw"  # a unit-less number used as-is (beta, an exit multiple in turns)


class FactDriverError(Exception):
    """A fact cannot be safely injected as the requested driver — unparseable
    token, no observations, an incompatible/absent unit, or an out-of-bounds
    converted value. Carries a user-facing message; the route maps it to 422."""


@dataclass(frozen=True, slots=True)
class DriverField:
    """One injectable RedesignInputs lever.

    ``family`` drives the unit conversion + the kind of fact accepted;
    ``min_value``/``max_value`` are the inclusive sanity bounds in TARGET units
    (decimal ratios for PROPORTION, $M for MONEY, turns/plain for RAW). ``apply``
    is how the value lands on the inputs: ``scalar`` sets ``attr`` directly;
    ``segment_near``/``segment_term`` set EVERY segment's near/terminal growth
    (the uniform-shift the model already uses for scenario growth deltas).
    """

    key: str
    label: str
    family: str
    min_value: float
    max_value: float
    apply: str  # "scalar" | "segment_near" | "segment_term"
    attr: str | None  # RedesignInputs attribute for apply == "scalar"


# The injectable drivers. WACC is intentionally not here (no input cell — it is
# derived from the CAPM levers below; offering it would silently no-op or, worse,
# need a back-solve we don't do). Adding a new revenue SEGMENT is out of scope
# (S7); these segment-growth fields edit EXISTING segments only.
DRIVER_FIELDS: tuple[DriverField, ...] = (
    # --- profitability (decimal ratios) ---
    DriverField(
        "near_op_margin",
        "Near-term operating margin",
        PROPORTION,
        -0.5,
        0.95,
        "scalar",
        "near_op_margin",
    ),
    DriverField(
        "terminal_op_margin",
        "Terminal operating margin",
        PROPORTION,
        -0.5,
        0.95,
        "scalar",
        "terminal_op_margin",
    ),
    DriverField("tax_rate", "Tax rate", PROPORTION, 0.0, 0.6, "scalar", "tax_rate"),
    # --- WACC drivers (a "set WACC" fact targets one of these, never WACC) ---
    DriverField(
        "risk_free_rate", "Risk-free rate (10Y)", PROPORTION, 0.0, 0.2, "scalar", "risk_free_rate"
    ),
    DriverField(
        "equity_risk_premium",
        "Equity risk premium",
        PROPORTION,
        0.0,
        0.15,
        "scalar",
        "equity_risk_premium",
    ),
    DriverField(
        "cost_of_debt", "Pre-tax cost of debt", PROPORTION, 0.0, 0.3, "scalar", "cost_of_debt"
    ),
    DriverField("beta", "Beta (levered)", RAW, 0.0, 4.0, "scalar", "beta"),
    # --- terminal value ---
    DriverField(
        "exit_multiple", "Exit multiple (turns)", RAW, 0.5, 75.0, "scalar", "exit_multiple"
    ),
    DriverField(
        "terminal_growth_g",
        "Terminal growth (g)",
        PROPORTION,
        -0.05,
        0.1,
        "scalar",
        "terminal_growth_g",
    ),
    DriverField(
        "terminal_capex_da",
        "Terminal capex / D&A",
        PROPORTION,
        0.0,
        5.0,
        "scalar",
        "terminal_capex_da",
    ),
    # --- capex ($M) ---
    DriverField(
        "capex_2026_m", "2026 capex ($M)", MONEY, 0.0, 2_000_000.0, "scalar", "capex_2026_m"
    ),
    # --- segment growth (applies to EVERY segment uniformly) ---
    DriverField(
        "segment_growth_near",
        "Segment growth — near-term (all segments)",
        PROPORTION,
        -0.5,
        1.0,
        "segment_near",
        None,
    ),
    DriverField(
        "segment_growth_term",
        "Segment growth — terminal (all segments)",
        PROPORTION,
        -0.1,
        0.2,
        "segment_term",
        None,
    ),
)

DRIVER_FIELDS_BY_KEY: dict[str, DriverField] = {f.key: f for f in DRIVER_FIELDS}


def driver_field_options() -> list[dict[str, str]]:
    """``[{"key", "label"}]`` for the DIY field-mapping dropdown, in registry
    order (profitability → WACC drivers → terminal → capex → segment growth)."""
    return [{"key": f.key, "label": f.label} for f in DRIVER_FIELDS]


# --------------------------------------------------------------------------- #
# Units / scale converter — THE load-bearing correctness piece
# --------------------------------------------------------------------------- #
_PROPORTION_UNITS: frozenset[Unit] = frozenset({Unit.RATIO, Unit.PERCENT, Unit.BPS})


@dataclass(frozen=True, slots=True)
class ConvertedValue:
    """A fact value rescaled into a driver field's units, with a human note
    describing the scale change (for the result chip + provenance)."""

    value: float
    note: str
    source_unit: str | None
    target_unit: str | None


def _parse_unit(raw: str | None) -> Unit | None:
    """A stored unit string → :class:`Unit`, or None when absent/out-of-vocab."""
    if raw is None:
        return None
    try:
        return Unit(raw.strip().lower())
    except ValueError:
        return None


def _fmt_unit(unit: Unit) -> str:
    return {
        Unit.RATIO: "ratio",
        Unit.PERCENT: "percent",
        Unit.BPS: "bps",
        Unit.ACTUAL: "$",
        Unit.THOUSANDS: "$K",
        Unit.MILLIONS: "$M",
        Unit.BILLIONS: "$B",
        Unit.COUNT: "count",
    }.get(unit, unit.value)


def convert_to_driver(raw_value: float, raw_unit: str | None, field: DriverField) -> ConvertedValue:
    """Rescale ``raw_value`` (in ``raw_unit``) into ``field``'s target units.

    The correctness contract, in order:

    * A non-finite value is rejected (NaN/inf would poison the model).
    * PROPORTION / MONEY fields require a known, family-compatible source unit
      and rescale through :func:`~models.unit_convert.convert_unit`
      (percent 4.3 → ratio 0.043; $ → $M). An ABSENT unit is rejected, not
      guessed — the 4.3-vs-0.043 trap. A cross-family unit (e.g. a dollar level
      offered to a margin) is rejected rather than blindly rescaled.
    * RAW fields (beta, an exit multiple in turns) take the number as-is, but
      reject a percent/bps source (that fact is a rate — it belongs in a rate
      field, and using it raw would be a 100×/10000× error).
    * The converted value is bounds-checked against the field's sane range; an
      out-of-range value (which ``derive_over_under`` would silently map to
      ``None``, or which would value the model absurdly) is rejected here.

    Raises :class:`FactDriverError` with a user-facing message on any failure.
    """
    if not math.isfinite(raw_value):
        raise FactDriverError("the fact's value is not a finite number")
    src = _parse_unit(raw_unit)

    if field.family == RAW:
        if src is not None and src in (Unit.PERCENT, Unit.BPS):
            raise FactDriverError(
                f"{field.label} takes a plain number, but the fact is a "
                f"{src.value} rate — pick a rate/margin field instead"
            )
        value = raw_value
        note = "used as-is (no unit scaling)"
        target_unit: str | None = None
    else:
        target = Unit.RATIO if field.family == PROPORTION else Unit.MILLIONS
        if src is None:
            raise FactDriverError(
                "the fact has no stored unit, so it cannot be scaled safely "
                "(a percent stored as 4.3 vs 0.043 would mis-scale the model) — "
                "edit the DCF cell directly instead"
            )
        try:
            converted = convert_unit(Decimal(str(raw_value)), src, target)
        except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
            raise FactDriverError(f"cannot convert the fact value: {exc}") from exc
        if converted is None:
            wanted = "a rate / percentage" if field.family == PROPORTION else "a dollar amount"
            raise FactDriverError(
                f"{field.label} expects {wanted}, but the fact's unit is "
                f"{src.value} — incompatible units, refusing to mis-scale"
            )
        value = float(converted)
        note = f"{_fmt_unit(src)} → {_fmt_unit(target)}"
        target_unit = target.value

    if not field.min_value <= value <= field.max_value:
        raise FactDriverError(
            f"converted value {value:g} is outside the safe range for "
            f"{field.label} [{field.min_value:g}, {field.max_value:g}] — "
            "refusing to write a degenerate driver"
        )
    return ConvertedValue(
        value=value,
        note=note,
        source_unit=src.value if src is not None else None,
        target_unit=target_unit,
    )


# --------------------------------------------------------------------------- #
# Value resolution — latest fact value, honoring fact_overrides (S2)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ResolvedFact:
    """The latest stored value for a picked metric token + its provenance."""

    value: float
    unit: str | None
    period_end: str  # 'YYYY-MM-DD'
    source: str  # provenance source label (tier / doc type / 'override')
    fact_id: int | None
    load_key: str  # the resolved series name actually loaded (KPI variant / line_item)


def _resolve_kpi_load_name(conn: sqlite3.Connection, ticker: str, requested: str) -> str:
    """Map a de-fragmented representative KPI token to THIS ticker's richest
    stored variant — mirrors ``viewspec.engine._kpi_name_resolution`` exactly so
    the picked token loads the same series the DIY table showed. Falls back to
    the literal token when nothing in the group matches (the loader then returns
    [] and the caller surfaces 'no observations')."""
    target = kpi_group_key(requested)
    try:
        rows = conn.execute(
            "SELECT kd.name AS name, COUNT(*) AS n "
            "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
            "WHERE kf.ticker = ? GROUP BY kd.name",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return requested
    cands = [(str(r["name"]), int(r["n"])) for r in rows if kpi_group_key(str(r["name"])) == target]
    if not cands:
        return requested
    # Most observations wins; ties broken shortest then alphabetical.
    return min(cands, key=lambda c: (-c[1], len(c[0]), c[0]))[0]


def _latest(series: list[SourcedObservation]) -> SourcedObservation | None:
    """The most-recent observation (loaders return ascending period_end)."""
    return series[-1] if series else None


def _override_only_latest(
    db_path: Path, *, ticker: str, fact_kind: str, fact_key: str, user_id: str
) -> ResolvedFact | None:
    """Latest active ``replace`` override for a fact that has NO base row (a
    company-doc figure FMP never carried — pickable since S5). The scalar loaders
    only overlay overrides onto EXISTING rows, so this reaches the override-only
    case they can't. None when there is no such override."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        candidates = [
            ov
            for ov in get_active_overrides(
                conn, ticker=ticker, fact_kind=fact_kind, user_id=user_id
            )
            if ov.fact_key == fact_key
            and ov.action == OverrideAction.REPLACE.value
            and ov.value is not None
        ]
    finally:
        conn.close()
    if not candidates:
        return None
    ov = max(candidates, key=lambda o: o.period_end)
    return ResolvedFact(
        value=float(ov.value) if ov.value is not None else 0.0,
        unit=ov.unit,
        period_end=ov.period_end,
        source=ov.source_doc_type,
        fact_id=None,
        load_key=fact_key,
    )


def resolve_fact_value(
    metric: MetricRef,
    *,
    ticker: str,
    repo_root: Path,
    db_path: Path,
    user_id: str = DEFAULT_USER_ID,
) -> ResolvedFact:
    """The LATEST stored value for a picked metric on ``ticker``, honoring
    company-doc ``fact_overrides`` precedence (S2).

    Reads through the same ``_with_provenance`` loaders the ask/engine read path
    uses (so a ``replace`` override on a base row wins, durably); for a KPI the
    representative token is first resolved to this ticker's richest variant. When
    no base series exists, an override-only fact is consulted. Raises
    :class:`FactDriverError` when nothing resolves.
    """
    t = ticker.upper()
    if metric.domain == "kpi":
        # Resolve the representative token to this ticker's own variant before
        # loading (the engine's per-ticker de-fragmentation), with a short-lived
        # connection just for the name lookup.
        load_key = metric.key
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                conn.row_factory = sqlite3.Row
                try:
                    load_key = _resolve_kpi_load_name(conn, t, metric.key)
                finally:
                    conn.close()
            except sqlite3.Error:
                load_key = metric.key
        series = load_kpi_series_with_provenance(
            t, load_key, repo_root, db_path=db_path, period_types=_ALL_FACT_PERIOD_TYPES
        )
        obs = _latest(series)
        if obs is None:
            ov = _override_only_latest(
                db_path, ticker=t, fact_kind=KPI, fact_key=load_key, user_id=user_id
            )
            if ov is not None:
                return ov
            raise FactDriverError(f"no observations for KPI '{metric.key}' on {t}")
        fact_id = obs.provenance.get("fact_id")
        return ResolvedFact(
            value=obs.value,
            unit=obs.unit,
            period_end=str(obs.period_end)[:10],
            source=str(obs.provenance.get("source") or "unknown"),
            fact_id=int(fact_id) if isinstance(fact_id, int) else None,
            load_key=load_key,
        )

    if metric.domain == "fin":
        series = load_financial_series_with_provenance(
            t, metric.key, repo_root, db_path=db_path, period_types=_ALL_FACT_PERIOD_TYPES
        )
        obs = _latest(series)
        if obs is None:
            ov = _override_only_latest(
                db_path, ticker=t, fact_kind=FINANCIAL_FACT, fact_key=metric.key, user_id=user_id
            )
            if ov is not None:
                return ov
            raise FactDriverError(f"no observations for '{metric.key}' on {t}")
        fact_id = obs.provenance.get("fact_id")
        return ResolvedFact(
            value=obs.value,
            unit=obs.unit,
            period_end=str(obs.period_end)[:10],
            source=str(obs.provenance.get("source") or "unknown"),
            fact_id=int(fact_id) if isinstance(fact_id, int) else None,
            load_key=metric.key,
        )

    # seg: period-level provenance; no override-only fallback (segment overrides
    # are record/cell-level and reconstructed differently).
    dims = [(metric.dim_type or "", metric.dim_name or "")]
    seg_series = load_segment_junction_series_with_provenance(
        t, dims, metric.key, repo_root, db_path=db_path, period_types=_ALL_FACT_PERIOD_TYPES
    )
    obs = _latest(seg_series)
    if obs is None:
        raise FactDriverError(f"no observations for segment '{metric.label}' on {t}")
    return ResolvedFact(
        value=obs.value,
        unit=obs.unit,
        period_end=str(obs.period_end)[:10],
        source=str(obs.provenance.get("source") or "unknown"),
        fact_id=None,
        load_key=metric.key,
    )


# --------------------------------------------------------------------------- #
# Apply to inputs — produce the edited (frozen) RedesignInputs
# --------------------------------------------------------------------------- #
def apply_to_inputs(
    inp: redesign_mod.RedesignInputs, field: DriverField, value: float
) -> redesign_mod.RedesignInputs:
    """Return a new ``RedesignInputs`` with ``field`` set to ``value``.

    Frozen in, frozen out — never a mutation. A scalar field replaces its single
    attribute; a segment-growth field sets EVERY segment's near/terminal growth
    to ``value`` (the uniform shift, matching the scenario-delta mechanic).
    """
    if field.apply == "scalar":
        if field.attr is None:  # pragma: no cover - registry invariant
            raise FactDriverError(f"driver field {field.key!r} has no target attribute")
        return replace(inp, **{field.attr: value})
    if field.apply == "segment_near":
        return replace(inp, near_growth_by_segment={s: value for s in inp.segments})
    if field.apply == "segment_term":
        return replace(inp, terminal_growth_by_segment={s: value for s in inp.segments})
    raise FactDriverError(f"unknown driver application {field.apply!r}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Durable provenance — note the fact lineage on the assumptions JSON (S6 #5)
# --------------------------------------------------------------------------- #
def record_driver_provenance(
    assumptions_path: Path,
    *,
    field_key: str,
    payload: dict[str, object],
    today: date | None = None,
) -> bool:
    """Record "this driver came from fact <id>" under
    ``redesign.driver_provenance[field_key]`` in the assumptions JSON.

    Additive and best-effort: it augments (never replaces) the existing block,
    survives ``sync_assumptions_json`` (which only rewrites the known numeric
    keys), and complements the existing override ledger (which already marks the
    cell "user-edited, overridden from Opus X"). Returns True on a successful
    write, False when there is no assumptions JSON / redesign block to annotate
    or the write fails — a provenance miss must never fail the injection itself.
    """
    if not assumptions_path.exists():
        return False
    try:
        raw: object = json.loads(assumptions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    data = cast("dict[str, object]", raw)
    block = data.get("redesign")
    if not isinstance(block, dict):
        return False
    rd = cast("dict[str, object]", block)
    existing = rd.get("driver_provenance")
    prov = cast("dict[str, object]", existing) if isinstance(existing, dict) else {}
    entry = dict(payload)
    entry["injected_on"] = (today or date.today()).isoformat()
    prov[field_key] = entry
    rd["driver_provenance"] = prov
    try:
        assumptions_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        return False
    return True

"""Phase-1 Wave-3 DCF assumption-tweak extractor + orchestrator (behind the gate).

The RISKIEST generation seam. A what-if wondering ("what if NU grows 30% not 40%?")
is turned into ONE bounded assumption tweak ``{param, new_value}`` that the
DETERMINISTIC DCF engine recomputes. The LLM emits ONLY the edit -- NEVER a dollar,
fair value, price target, upside, or per-share number; the Python engine produces
every number and that recompute IS the oracle. The resulting ``DcfRunRow`` is held
as an inert ``kind='dcf'`` proposal (``draft_dcf_proposal``), never upserted live
until approve clears the higher-bar gate.

``param`` is a key in ``dcf.fact_drivers.DRIVER_FIELDS_BY_KEY`` (the injectable
levers); ``new_value`` is validated against that lever's inclusive ``[min, max]``
bounds HERE -- ``apply_to_inputs`` does NOT range-check, so this is the only bound.
The extractor is WEB-LESS and holds no writer (the sole input is the owner's own
wondering). The recompute + persist stay separate (the orchestrator), so no single
context both reads untrusted input and writes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from dcf.fact_drivers import DRIVER_FIELDS, DRIVER_FIELDS_BY_KEY
from dcf.provenance import build_effective_provenance

# The injected structured caller (tests): wondering -> raw extracted dict.
DcfTweakCall = Callable[[str], "dict[str, object]"]

_UNIT_BY_FAMILY: dict[str, str] = {
    "proportion": "decimal ratio (0.30 = 30%)",
    "money": "$ millions",
    "raw": "plain number (e.g. beta, exit multiple in turns)",
}


def _param_menu() -> str:
    return "\n".join(
        f"- {f.key} — {f.label} [{_UNIT_BY_FAMILY.get(f.family, 'number')}]" for f in DRIVER_FIELDS
    )


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def extract_dcf_tweak(
    wondering: str, ticker: str | None = None, *, call: DcfTweakCall | None = None
) -> dict[str, object] | None:
    """The governed ``dcf_assumption_extract`` extractor: parse a what-if wondering
    into ONE bounded ``{param, new_value}`` tweak, or None.

    The LLM chooses ``param`` from the driver registry and gives ``new_value`` in the
    param's NATIVE unit -- it emits NO valuation number. Returns None unless the model
    is HIGH-confidence AND ``param`` is a known driver AND ``new_value`` is finite AND
    within the driver's inclusive ``[min, max]`` bounds (``apply_to_inputs`` never
    range-checks, so this is the ONLY bound). ``call`` is the injected caller (tests);
    the default runs the governed purpose via ``call_llm_structured`` (web-less).
    """
    wondering = (wondering or "").strip()
    if not wondering:
        return None
    obj = call(wondering) if call is not None else _default_dcf_call(wondering, ticker)
    if str(obj.get("confidence") or "").strip().lower() != "high":
        return None  # only a fully-unambiguous read is allowed to move a valuation
    param = str(obj.get("param") or "").strip()
    field = DRIVER_FIELDS_BY_KEY.get(param)
    if field is None:
        return None  # off-registry token
    new_value = _finite(obj.get("new_value"))
    if new_value is None or not (field.min_value <= new_value <= field.max_value):
        return None  # missing/non-finite/out-of-bounds -> refuse the tweak
    return {"param": param, "new_value": new_value}


_DCF_EXTRACT_PROMPT = (
    "You extract at most ONE bounded assumption tweak from an investor's what-if "
    "question about a discounted-cash-flow (DCF) model. You output ONLY the edit -- you "
    "NEVER produce any dollar value, fair value, price target, valuation, upside, or "
    "per-share number. A separate deterministic engine recomputes every number from "
    "your edit.\n\n"
    "Return JSON ONLY, no prose, no markdown fences:\n"
    '{"param": "<one token copied EXACTLY from the ALLOWED PARAMS list, or empty '
    'string>", "new_value": <a single decimal number in the param native unit, or '
    'null>, "confidence": "high" | "low"}\n\n'
    "ALLOWED PARAMS (copy the token verbatim; each line is `token — meaning [native "
    f"unit]`):\n{_param_menu()}\n\n"
    "Rules:\n"
    "- Choose the SINGLE param the question most directly changes. If the question "
    "changes none of these, is ambiguous between several, asks for a dollar/price/"
    "valuation output, or asks for anything other than one of the ALLOWED PARAMS, "
    'return {"param": "", "new_value": null, "confidence": "low"}.\n'
    "- new_value is the ABSOLUTE new level in the param's NATIVE UNIT, never a delta and "
    "never a percent-of-anything. Rates/margins/growth are DECIMAL ratios: 'grows 30%' "
    "-> 0.30 (NOT 30). 'tax rate of 21%' -> 0.21. 'terminal growth 2.5%' -> 0.025. Beta "
    "and exit multiple are plain numbers ('beta 1.2' -> 1.2, 'exit at 14x' -> 14). Capex "
    "is in $millions.\n"
    "- 'grows X% instead of Y%' is a growth question: pick segment_growth_near "
    "(near-term) unless the question explicitly says terminal/long-run, then "
    "segment_growth_term. Growth applies uniformly to all segments.\n"
    "- Do NOT invent a value the question doesn't state. If the question names a param "
    "but no number ('what if margins improve?'), return confidence 'low' and new_value "
    "null.\n"
    "- confidence is 'high' only when both the param and the number are unambiguous.\n\n"
)


def _default_dcf_call(wondering: str, ticker: str | None) -> dict[str, object]:
    """Default structured caller: the governed ``dcf_assumption_extract`` purpose.
    Web-less. Degrades to {} on a double-parse failure; a budget/setup hard stop
    propagates as config (per the repo's LLM exception policy)."""
    from llm.contracts import DCF_TWEAK_SCHEMA
    from llm.structured import StructuredParseError, call_llm_structured

    prompt = (
        f"{_DCF_EXTRACT_PROMPT}Company: {ticker or 'n/a'}\nWhat-if question: {wondering}\nJSON:"
    )
    try:
        obj = call_llm_structured(
            prompt,
            purpose="dcf_assumption_extract",
            expect="object",
            required_keys=("param", "new_value"),
            schema=DCF_TWEAK_SCHEMA,
        )
    except StructuredParseError:
        return {}
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


# --- the deterministic recompute (the ORACLE) + the orchestrator ----------------------

RecomputeFn = Callable[..., "dict[str, object] | None"]
ExtractFn = Callable[..., "dict[str, object] | None"]


def recompute_row_from_inputs(
    inp: Any,
    ticker: str,
    *,
    tweak: dict[str, object] | None = None,
    valuation_date: date | None = None,
    live_price: float | None = None,
    live_price_at: datetime | None = None,
    mos_bar: float | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    """The CRUX mapping: run the PURE ``dcf.redesign.value`` over an (already-tweaked)
    ``RedesignInputs`` and build the proposed ``dcf_runs`` row dict that
    ``draft_dcf_proposal`` consumes -- mirroring the canonical refresh mapping
    (``execution/refresh_dcf``). The LLM contributes NO number here; every value is the
    engine's. ``live_price`` / ``mos_bar`` are SIDE-inputs (the caller injects them).

    Unit contract (easy to get wrong, pinned by the round-trip test):
      npv                = rv.operating_value_usd_m   (enterprise value, USD millions)
      npv_per_share      = rv.value_per_share_usd      (USD, FX-converted)
      shares_outstanding = rv.diluted_shares_m * 1e6   (ABSOLUTE count, not millions)
      horizon_years      = len(rv.fcff_stream_m)        (the forecast horizon)
      over_under_pct     -- NOT set here; derived at persist.upsert from live/fair.
    May raise ``dcf.redesign.RedesignError`` for an un-valuable assumption set.
    """
    from dcf import redesign

    rv = redesign.value(inp)
    snapshot = {
        "source": "dcf_assumption_tweak",
        "tweak": tweak or {},
        "wacc": rv.wacc,
        "operating_value_usd_m": rv.operating_value_usd_m,
        "diluted_shares_m": rv.diluted_shares_m,
        "terminal_method": rv.terminal_method,
        "terminal_basis": rv.terminal_basis,
        "exit_multiple": rv.exit_multiple,
        "live_price": live_price,
        "live_price_at": live_price_at.isoformat() if live_price_at else None,
        "mos_bar_used": mos_bar,
    }
    return {
        "ticker": ticker.strip().upper(),
        "valuation_date": (valuation_date or date.today()).isoformat(),
        "horizon_years": len(rv.fcff_stream_m),
        "wacc": rv.wacc,
        "npv": rv.operating_value_usd_m,
        "npv_per_share": rv.value_per_share_usd,
        "shares_outstanding": rv.diluted_shares_m * 1_000_000.0,
        "currency": "USD",
        "live_price": live_price,
        "live_price_at": live_price_at.isoformat() if live_price_at else None,
        "mos_bar_used": mos_bar,
        "assumption_snapshot_json": json.dumps(snapshot),
        "notes": "dcf assumption tweak (recomputed)",
        "run_id": run_id,
    }


def _side_inputs(
    repo_root: Path, ticker: str
) -> tuple[float | None, datetime | None, float | None]:
    """Best-effort live price (network) + margin-of-safety bar (local JSON). Any failure
    yields None -- a missing side-input never blocks the recompute (over_under simply
    stays NULL, per the persist chokepoint)."""
    live_price: float | None = None
    live_at: datetime | None = None
    try:
        from sources.price import read_live_price

        live = read_live_price(repo_root, ticker)
        if live is not None:
            live_price, live_at = float(live.price), live.fetched_at
    except Exception:
        pass
    mos_bar: float | None = None
    try:
        raw: object = json.loads(
            (repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json").read_text(
                encoding="utf-8"
            )
        )
        if isinstance(raw, dict):
            mos_bar = _finite(cast("dict[str, object]", raw).get("mos_bar"))
    except Exception:
        pass
    return live_price, live_at, mos_bar


def default_recompute(
    ticker: str, param: str, new_value: float, *, repo_root: Path | None = None
) -> dict[str, object] | None:
    """The real wiring: load the ticker's redesign workbook, apply the bounded tweak,
    and recompute. Returns None when the workbook is absent/non-redesign or the tweaked
    assumptions are un-valuable -- never raises to the orchestrator. Needs ``repo_root``
    (the workbook + side-inputs are file/network resources); a network live-price miss
    degrades to a NULL over_under, not a failure."""
    from dcf import redesign
    from dcf.fact_drivers import apply_to_inputs

    field = DRIVER_FIELDS_BY_KEY.get(param)
    if field is None or repo_root is None:
        return None
    workbook = Path(repo_root) / "dcf" / f"{ticker.upper()}.xlsx"
    try:
        inp = redesign.read_inputs(workbook)
    except Exception:
        return None  # missing / corrupt / non-redesign workbook -> no recompute
    if inp is None:
        return None
    tweaked = apply_to_inputs(inp, field, new_value)
    live_price, live_at, mos_bar = _side_inputs(Path(repo_root), ticker)
    try:
        row = recompute_row_from_inputs(
            tweaked,
            ticker,
            tweak={"param": param, "new_value": new_value},
            live_price=live_price,
            live_price_at=live_at,
            mos_bar=mos_bar,
        )
        snapshot_json = str(row["assumption_snapshot_json"])
        row["provenance"] = asdict(
            build_effective_provenance(
                ticker=ticker,
                repo_root=Path(repo_root),
                workbook_path=workbook,
                assumption_snapshot_json=snapshot_json,
                engine_version="redesign_assumption_tweak_v1",
                source_paths=(
                    (
                        "thesis_holdings",
                        Path(repo_root) / "micro_thesis" / "holdings" / f"{ticker.upper()}.json",
                    ),
                ),
            )
        )
        return row
    except redesign.RedesignError:
        return None  # un-valuable assumption set (e.g. WACC <= terminal g)


def draft_dcf_tweak_proposal(
    *,
    wondering: str,
    ticker: str,
    old_npv_per_share: float | None = None,
    evidence_json: str = "[]",
    adversarial_verdict: str | None = None,
    note_id: int | None = None,
    task_id: int | None = None,
    repo_root: Path | None = None,
    db_path: Path | str | None = None,
    extract_fn: ExtractFn = extract_dcf_tweak,
    recompute_fn: RecomputeFn | None = None,
) -> int | None:
    """Orchestrate: wondering -> extract ONE bounded tweak -> deterministic recompute ->
    inert ``kind='dcf'`` proposal (``draft_dcf_proposal``). Returns the proposal id, or
    None (no valid tweak / no valuable recompute). The recompute is the ORACLE; the LLM
    contributes only ``{param, new_value}``. Nothing is written live -- the proposal is
    inert and the upsert waits behind the higher-bar gate on approve.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    tweak = extract_fn(wondering, ticker)
    if not isinstance(tweak, dict) or "param" not in tweak or "new_value" not in tweak:
        return None
    param = str(tweak["param"])
    new_value = _finite(tweak["new_value"])
    if new_value is None:
        return None
    recompute = recompute_fn or default_recompute
    proposed_row = recompute(ticker, param, new_value, repo_root=repo_root)
    if not isinstance(proposed_row, dict):
        return None

    from research.dcf_artifact import draft_dcf_proposal

    return draft_dcf_proposal(
        ticker=ticker,
        proposed_row=proposed_row,
        old_npv_per_share=old_npv_per_share,
        evidence_json=evidence_json,
        adversarial_verdict=adversarial_verdict,
        note_id=note_id,
        task_id=task_id,
        db_path=db_path,
    )

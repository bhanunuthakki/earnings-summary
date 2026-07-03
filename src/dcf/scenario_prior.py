"""scenario_prior — per-name Bull/Base/Bear scenario weights, LLM-set with rationale.

``dcf.scenario_reward`` weights the three DCF fair values (bull/base/bear) into a
probability-weighted expected return that three allocation surfaces consume. Until
now that used a single fixed global prior (25 / 50 / 25) for every name — a
symmetric stance that throws away the fact that some theses are downside-skewed
(fragile, high-multiple, execution-heavy) and others carry cheap upside optionality.

This module is the governed LLM seam that sets a PER-NAME prior: bull/base/bear
weights + a 1-2 sentence rationale, grounded in the analyst's own thesis / bear /
KPI anchors (``llm.anchors``). The owner's decision, verbatim: "LLM can
automatically take the call here with a rationale in the brief, I'll ask for edits
or edit directly in the DCF as needed." So the LLM proposes; the owner's workbook
edit always wins downstream (PR D), and the value-of-record consumers fall back to
the global prior whenever no per-name prior is on file.

Governance (this is RISKY-adjacent — it moves allocation):

* **Operational recipe** — routed via ``LLM_MODELS["scenario_prior"]`` +
  ``prompt_versions``; the structured call raises ``StructuredParseError`` on
  unusable JSON, which the operational wrapper (:func:`generate_scenario_prior`)
  degrades to the global prior (a hard stop — budget/setup — propagates).
* **A non-degenerate simplex is enforced in code, not trusted from the model**:
  weights must sum to 1 (renormalized within tolerance), the Base leg stays the
  plurality (>= :data:`_MIN_BASE`), and no leg collapses (< :data:`_MIN_WEIGHT`)
  or dominates (> :data:`_MAX_WEIGHT`). A model answer that violates this is
  rejected to the global prior, so a single bad call can never swing allocation
  into an all-in tail bet.
* **Evaluated** — the ``scenario_prior`` golden set (``evals/golden``) grades the
  directional skew + rationale against pinned thesis/bear anchors, wired through
  the 4-registry lockstep (run_llm_evals / evals_panel / coverage / prompt_versions).

The seam is dependency-injected (``call=``) so it is unit-tested with no LLM.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import cast

log = logging.getLogger(__name__)

PURPOSE = "scenario_prior"

# Non-degenerate simplex bounds — enforced in code, never trusted from the model.
# A per-name prior may tilt the tails but the Base leg must stay the plurality and
# no leg may collapse or dominate, so a single LLM call can't turn the reward into
# an all-in tail bet that swings the allocation surfaces.
_MIN_WEIGHT = 0.05
_MAX_WEIGHT = 0.90
_MIN_BASE = 0.30
_SUM_TOL = 0.03  # accept + renormalize when the three weights sum within tol of 1
_RATIONALE_CAP = 400  # hard char cap on the persisted rationale

# The documented global fallback — identical to dcf.scenario_reward's prior, kept
# in sync by the guard test so the "no per-name prior" reward is exactly today's.
GLOBAL_WEIGHTS: dict[str, float] = {"bull": 0.25, "base": 0.50, "bear": 0.25}


@dataclass(frozen=True, slots=True)
class ScenarioPrior:
    """A per-name Bull/Base/Bear prior. ``set_by`` records provenance: ``"llm"``
    (a grounded per-name call), ``"owner"`` (a workbook/dashboard edit — PR D), or
    ``"global"`` (the symmetric fallback, no per-name call on file). ``as_of`` is
    an ISO date for cache stability + display."""

    bull: float
    base: float
    bear: float
    rationale: str
    set_by: str
    as_of: str

    def weights(self) -> dict[str, float]:
        return {"bull": self.bull, "base": self.base, "bear": self.bear}

    @property
    def is_per_name(self) -> bool:
        """True when a real per-name prior is on file (LLM or owner), i.e. not the
        global fallback — the signal the reward path uses to prefer it."""
        return self.set_by in ("llm", "owner")


def global_prior(today: date | None = None) -> ScenarioPrior:
    """The symmetric 25/50/25 fallback used when no per-name prior is on file."""
    d = (today or date.today()).isoformat()
    return ScenarioPrior(
        bull=GLOBAL_WEIGHTS["bull"],
        base=GLOBAL_WEIGHTS["base"],
        bear=GLOBAL_WEIGHTS["bear"],
        rationale="Symmetric default prior — the tails carry a quarter of the mass each; "
        "no per-name call on file.",
        set_by="global",
        as_of=d,
    )


# The injectable LLM seam: prompt -> parsed JSON object (the shape
# call_llm_structured returns for expect="object"). Dependency-injected so the
# generator is unit-testable with no live call.
ScenarioPriorCall = Callable[[str], dict[str, object]]


_PROMPT = """You are setting the probability weights for the three DCF scenarios \
(Bull / Base / Bear) for {ticker}, grounded ONLY in the analyst's own thesis and \
bear-case anchors below.

The DCF already computes three fair values per share (bull / base / bear). These \
weights decide how much probability mass each carries when the platform computes \
the probability-weighted expected value and the reward the allocation surfaces \
consume. So the weights must reflect the ASYMMETRY of THIS name's thesis:

- Tilt toward BEAR when the thesis is fragile — a demanding multiple, execution or \
regulatory risk, a single-point-of-failure driver, or a bear case the analyst \
takes seriously.
- Tilt toward BULL when the downside looks well-protected and the base case is \
conservative relative to real optionality.
- Stay near the symmetric default when the range is genuinely balanced.

{anchor_block}

Rules (a violated rule means your answer is discarded and the symmetric default \
is used instead):
- Output three DECIMAL weights that sum to 1.0 (e.g. 0.30 / 0.50 / 0.20).
- The Base leg must remain the plurality: base >= 0.30.
- No leg below 0.05 — every scenario keeps some mass.
- The symmetric default is 0.25 / 0.50 / 0.25; move OFF it only where the anchors \
justify a specific skew, and name that factor in the rationale.

Return ONLY this JSON object, nothing else:
{{"bull": <decimal>, "base": <decimal>, "bear": <decimal>, "rationale": "<1-2 \
sentences naming the specific thesis/bear factor that justifies the skew>"}}"""


def build_prompt(ticker: str, anchor_block: str) -> str:
    """The scenario_prior prompt for one name. ``anchor_block`` is the composed +
    spotlighted thesis/bear/KPI context (``llm.anchors.compose_anchor_block``);
    embedding it as the only grounding keeps the call thesis-anchored."""
    return _PROMPT.format(ticker=ticker.upper(), anchor_block=anchor_block)


def _num(payload: dict[str, object], key: str) -> float | None:
    v = payload.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def coerce_weights(payload: dict[str, object]) -> tuple[float, float, float] | None:
    """Extract + validate a non-degenerate (bull, base, bear) simplex from a model
    payload, or ``None`` when it can't be made valid.

    Tolerates a model that answers in percent (25 / 50 / 25) instead of decimals by
    detecting a ~100 sum; renormalizes a near-1 sum to exactly 1; then enforces the
    plurality-Base + no-collapsed/dominant-leg bounds. Deliberately strict — an
    out-of-bounds answer returns None so the caller uses the global prior rather
    than a degenerate one."""
    bull = _num(payload, "bull")
    base = _num(payload, "base")
    bear = _num(payload, "bear")
    if bull is None or base is None or bear is None:
        return None
    if bull < 0 or base < 0 or bear < 0:
        return None
    s = bull + base + bear
    if 90.0 <= s <= 110.0:  # answered in percent — rescale to decimals
        bull, base, bear, s = bull / 100.0, base / 100.0, bear / 100.0, s / 100.0
    if abs(s - 1.0) > _SUM_TOL or s <= 0:
        return None
    bull, base, bear = bull / s, base / s, bear / s  # renormalize to exactly 1
    for w in (bull, base, bear):
        if not (_MIN_WEIGHT <= w <= _MAX_WEIGHT):
            return None
    if base < _MIN_BASE:
        return None
    return bull, base, bear


def _default_call(ticker: str) -> ScenarioPriorCall:
    """The governed structured call for PURPOSE, bound to a ticker. Isolated so the
    generator can be dependency-injected in tests with no live LLM."""

    def call(prompt: str) -> dict[str, object]:
        from llm.structured import call_llm_structured

        payload = call_llm_structured(
            prompt,
            purpose=PURPOSE,
            ticker=ticker,
            expect="object",
            required_keys=("bull", "base", "bear", "rationale"),
        )
        return cast("dict[str, object]", payload)

    return call


def propose_scenario_prior(
    ticker: str,
    *,
    anchor_block: str,
    today: date | None = None,
    call: ScenarioPriorCall | None = None,
) -> ScenarioPrior:
    """Propose a per-name prior from the anchor block. RAISES ``StructuredParseError``
    on unusable JSON (the eval scores that as a call failure; the operational
    wrapper degrades it) and lets hard stops propagate. A parseable-but-degenerate
    simplex, or an empty rationale, degrades to the global prior in place (set_by
    stays ``"global"``), so a bad-but-well-formed answer can't move allocation."""
    the_call = call if call is not None else _default_call(ticker)
    payload = the_call(build_prompt(ticker, anchor_block))
    weights = coerce_weights(payload)
    rationale_raw = payload.get("rationale")
    rationale = rationale_raw.strip() if isinstance(rationale_raw, str) else ""
    if weights is None or not rationale:
        log.warning(
            {
                "event": "scenario_prior_rejected_to_global",
                "ticker": ticker,
                "reason": "degenerate weights" if weights is None else "empty rationale",
            }
        )
        return global_prior(today)
    bull, base, bear = weights
    d = (today or date.today()).isoformat()
    return ScenarioPrior(
        bull=bull,
        base=base,
        bear=bear,
        rationale=rationale[:_RATIONALE_CAP],
        set_by="llm",
        as_of=d,
    )


def generate_scenario_prior(
    ticker: str,
    *,
    anchor_block: str,
    today: date | None = None,
    call: ScenarioPriorCall | None = None,
) -> ScenarioPrior:
    """Operational entry point: :func:`propose_scenario_prior`, but degrading a
    ``StructuredParseError`` to the global prior so a transient bad response never
    blocks the pipeline (a budget/setup hard stop still propagates — it is config,
    not parse quality). An empty ``anchor_block`` returns the global prior WITHOUT
    spending a call — there is nothing to ground a per-name skew on."""
    from llm.structured import StructuredParseError

    if not anchor_block.strip():
        return global_prior(today)
    try:
        return propose_scenario_prior(ticker, anchor_block=anchor_block, today=today, call=call)
    except StructuredParseError:
        log.warning({"event": "scenario_prior_parse_failed_to_global", "ticker": ticker})
        return global_prior(today)


def anchor_block_for_ticker(ticker: str, repo_root: object) -> str:
    """Compose the analyst's thesis/bear/priors anchor block for ``ticker`` — the
    same grounding text :func:`set_scenario_prior_for_ticker` feeds the LLM.

    Split out so a cadence/refresh script (:mod:`execution.set_scenario_priors`
    ``--only-changed``) can hash this text to decide whether the thesis/bear-case
    input actually changed since the last LLM call, WITHOUT spending a call just
    to find out — mirrors the ``dcf.compute.peer_selection`` cache-invalidation
    pattern (hash the inputs, skip the call on a hit)."""
    from pathlib import Path

    from llm.anchors import (
        compose_anchor_block,
        load_bear_anchor,
        load_priors_anchor,
        load_thesis_anchor,
    )

    root = repo_root if isinstance(repo_root, Path) else Path(str(repo_root))
    return compose_anchor_block(
        load_thesis_anchor(root, ticker),
        load_bear_anchor(root, ticker),
        "",
        load_priors_anchor(root, ticker),
    )


def anchor_inputs_sha256(anchor_block: str) -> str:
    """The stable hash of an anchor block, persisted alongside the prior
    (``scenario_prior.inputs_sha256``) so a later cadence run can tell whether
    the thesis/bear-case text changed without re-calling the LLM."""
    return hashlib.sha256(anchor_block.encode("utf-8")).hexdigest()


def set_scenario_prior_for_ticker(
    ticker: str,
    repo_root: object,
    *,
    today: date | None = None,
    call: ScenarioPriorCall | None = None,
) -> ScenarioPrior:
    """Compose the analyst's thesis/bear/priors anchors for ``ticker`` and generate
    a per-name prior. The production entry point (PR D persists it); degrades to the
    global prior when no anchors exist for the name."""
    anchor_block = anchor_block_for_ticker(ticker, repo_root)
    return generate_scenario_prior(ticker, anchor_block=anchor_block, today=today, call=call)

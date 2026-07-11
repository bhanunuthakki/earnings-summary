"""The five rotating per-name attack lenses (monthly_red_team.md Phase 2).

Lens rotation is DETERMINISTIC, not stateful: ``lens_for(ticker, month_index)``
derives the lens from a stable hash of the ticker plus the calendar month
index, so no rotation-state table is needed and the choice is reproducible
from (ticker, month) alone. ``month_index`` increases by exactly 1 every
calendar month, so consecutive months always land on a different lens (the
directive's "the same name must not get the same lens twice in a row" —
guaranteed because +1 mod 5 is never 0). Python's built-in ``hash()`` is
process-salted (``PYTHONHASHSEED``) and would NOT be reproducible across runs,
so this uses a fixed ``sha256``-derived seed instead.

Evidence is assembled entirely in CODE (thesis anchor, verdict, weight, latest
DCF over/under — Layer-3 discipline: no business logic riding in the prompt
that code can compute). The LLM's only job is the adversarial judgment.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from bear_lint import BearLintFinding
from llm.anchors import load_thesis_anchor
from llm.structured import StructuredParseError, call_llm_structured
from model_provenance.basis import dcf_basis
from redteam.models import LENS_NAMES, RedTeamLLMItem

log = logging.getLogger(__name__)

_HOLDINGS_DIRNAME = ("micro_thesis", "holdings")

# LLM purpose for every per-name lens call (LLM_MODELS + prompt_versions).
PURPOSE = "red_team_attack"


def load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read ``micro_thesis/holdings/<TICKER>.json`` defensively. ``None`` on
    any read/parse failure so the pack degrades to thesis-anchor-only.

    Public (not module-private) because ``cross_book.py`` reuses it for the
    factor-block pass's business-description one-liners — same package, one
    loader.
    """
    path = repo_root.joinpath(*_HOLDINGS_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass(slots=True, frozen=True)
class NameEvidencePack:
    """Everything one per-name lens call needs, assembled deterministically."""

    ticker: str
    weight_pct: float
    thesis_anchor_md: str  # composed via llm.anchors.load_thesis_anchor
    verdict: str | None
    key_driver: str | None
    # DCF-vs-market disagreement, when a dcf_runs row exists — None means "no
    # DCF on file", NOT "0% disagreement". Populated via model_provenance.basis.
    over_under_pct: float | None
    # Bear-realism lint (Phase 1 PR1, src/bear_lint.py — merged): the held
    # name's latest top-level DCF bear-scenario classification
    # ("missing" | "not_a_bear" | "shallow" | "ok") and its provenance
    # ("seed" | "thesis" | "owner"). None when no bear-lint finding is
    # available (e.g. no dcf_runs row, or the caller didn't pass one) —
    # degrades to omitting the evidence line rather than fabricating it.
    bear_status: str | None = None
    bear_provenance: str | None = None


def build_name_evidence_pack(
    repo_root: Path,
    conn: object,
    *,
    ticker: str,
    weight_pct: float,
    bear_finding: BearLintFinding | None = None,
) -> NameEvidencePack | None:
    """Assemble one held name's evidence pack. ``None`` when there is no
    holdings JSON on file at all (nothing to attack — the caller skips and
    tallies it, it never fabricates a thesis). ``bear_finding`` is the
    caller's pre-computed ``bear_lint.build_bear_lint`` row for this ticker
    (one book-wide lint pass, not one per name) — omitted when the caller has
    none (a DB without ``dcf_runs``, or bear_lint unavailable)."""
    import sqlite3

    payload = load_holdings_json(repo_root, ticker)
    if payload is None:
        return None
    thesis_anchor_md = load_thesis_anchor(repo_root, ticker)
    verdict = payload.get("verdict")
    key_driver = payload.get("key_driver")

    over_under_pct: float | None = None
    if isinstance(conn, sqlite3.Connection):
        try:
            basis = dcf_basis(conn, ticker)
        except Exception as exc:  # best-effort — a DCF read failure never blocks the pack
            log.debug({"event": "red_team_dcf_basis_failed", "ticker": ticker, "error": str(exc)})
            basis = None
        if basis is not None and basis.meta_json:
            try:
                meta = cast("dict[str, object]", json.loads(basis.meta_json))
                raw = meta.get("over_under_pct")
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    over_under_pct = float(raw)
            except (ValueError, TypeError):
                over_under_pct = None

    return NameEvidencePack(
        ticker=ticker.upper(),
        weight_pct=weight_pct,
        thesis_anchor_md=thesis_anchor_md,
        verdict=(verdict if isinstance(verdict, str) else None),
        key_driver=(key_driver if isinstance(key_driver, str) else None),
        over_under_pct=over_under_pct,
        bear_status=(bear_finding.status if bear_finding is not None else None),
        bear_provenance=(bear_finding.provenance if bear_finding is not None else None),
    )


def _deterministic_seed(ticker: str) -> int:
    """Stable (process-independent) integer seed for ``ticker`` — NOT Python's
    built-in ``hash()``, which is salted per process and would make the
    rotation non-reproducible run to run."""
    return int(hashlib.sha256(ticker.upper().encode("utf-8")).hexdigest(), 16)


def month_index_for(month: str) -> int:
    """``"2026-08"`` -> a monotonically increasing month counter
    (``year * 12 + month``). Only the delta between consecutive calls matters
    for the rotation guarantee, so the exact base is unimportant."""
    year_s, month_s = month.split("-", 1)
    return int(year_s) * 12 + int(month_s)


def lens_for(ticker: str, month_index: int) -> str:
    """The deterministic lens assignment for (ticker, month). See module
    docstring for the no-repeat-in-a-row guarantee."""
    idx = (_deterministic_seed(ticker) + month_index) % len(LENS_NAMES)
    return LENS_NAMES[idx]


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------

_LENS_FRAMING: dict[str, str] = {
    "shared_factor": (
        "ATTACK ANGLE: shared-factor risk. Identify the single macro/company "
        "factor (a commodity input, a geography, a regulator, a platform "
        "dependency, a credit cycle) that this name's thesis is MOST exposed "
        "to, and that plausibly also drives another position in the same "
        "book. State the factor explicitly and falsifiably — not \"the "
        "economy\" but a specific, checkable driver."
    ),
    "fx_translation": (
        "ATTACK ANGLE: FX / currency-translation risk. Identify how currency "
        "movements (local-currency revenue translated to USD, a peg or "
        "managed-float regime, a central bank's rate policy) could make "
        "reported results look like thesis progress or thesis breakage when "
        "the underlying operating trend hasn't actually moved. Be specific "
        "about the currency pair and the mechanism (translation vs "
        "transaction exposure)."
    ),
    "competitive_encroachment": (
        "ATTACK ANGLE: competitive encroachment. Name ONE specific, real "
        "competitor (or a specific new entrant category — an incumbent "
        "moving down-market, a well-funded startup, a platform owner "
        "verticalizing) that could erode the moat this thesis depends on, "
        "and the concrete mechanism (pricing, distribution, switching cost, "
        "network effect capture) by which it would show up in the numbers."
    ),
    "model_vs_market": (
        "ATTACK ANGLE: model vs. market disagreement. The fair-value model "
        "disagrees with the market price. Argue the MARKET's side: what does "
        "the market plausibly know or price in that the model does not — a "
        "structural risk the DCF assumptions understate, a growth/margin "
        "path that is optimistic versus the peer set, or a discount-rate "
        "input that is too low. Do not simply restate the bull case."
    ),
    "behavioral_consistency": (
        "ATTACK ANGLE: behavioral consistency. Compare the stated thesis "
        "conviction (verdict, weight-of-book, break conditions) against how "
        "an analyst under normal behavioral biases (anchoring to cost basis, "
        "sunk-cost, confirmation-seeking, position-size creep without a "
        "re-underwrite) would actually be treating this name. Ask whether "
        "the CURRENT size and break conditions are consistent with the "
        "CURRENT thesis conviction, or whether they're stale."
    ),
}

_ITEM_SCHEMA_INSTRUCTIONS = (
    "Return ONLY a JSON object with this exact shape:\n"
    '{"attack_md": "<2-4 sentences: the falsifiable claim, evidence-anchored '
    "to the facts above — a specific number, date, or named entity, not "
    'vague concern>", '
    '"question_md": "<ONE direct, falsifiable question the analyst must '
    'answer to refute or accept this attack>", '
    '"proposed_change_md": "<ONE concrete rule or scenario change — a new '
    "break-rule threshold, a bear-case assumption edit, a sizing-intent "
    'downside rung>", '
    '"severity": "<one of high, med, low — how much of the thesis breaks if '
    'this attack is right>"}\n\n'
    "Output the JSON object and nothing else — no markdown fences, no "
    "commentary, no prefatory prose. Be Stock-Market-Nerd-level specific: "
    "every claim must be checkable against the evidence given, never a "
    "generic macro worry."
)


def build_prompt(pack: NameEvidencePack, lens: str, *, other_holdings_line: str) -> str:
    """Compose the one-shot adversarial prompt for ``lens`` over ``pack``."""
    framing = _LENS_FRAMING.get(lens, _LENS_FRAMING["shared_factor"])
    verdict_line = f"Current verdict: {pack.verdict}." if pack.verdict else ""
    driver_line = f"Key driver: {pack.key_driver}." if pack.key_driver else ""
    weight_line = f"Book weight: {pack.weight_pct:.1%} of portfolio."
    dcf_line = (
        f"DCF model disagrees with the market by {pack.over_under_pct:+.1%} "
        "(fair value vs. live price)."
        if pack.over_under_pct is not None
        else "No DCF fair-value model on file for this name."
    )
    bear_line = ""
    if pack.bear_status is not None:
        prov = f", provenance {pack.bear_provenance}" if pack.bear_provenance else ""
        bear_line = f" Bear-realism lint: {pack.bear_status}{prov}."
    anchor = pack.thesis_anchor_md.strip() or "(no thesis anchor on file)"

    return (
        f"You are the monthly adversarial Red Team reviewing a held position, "
        f"{pack.ticker}, in a long-only equity portfolio. Your job is NOT to "
        f"summarize the thesis — it is to attack it.\n\n"
        f"{framing}\n\n"
        f"{weight_line} {verdict_line} {driver_line} {dcf_line}{bear_line}\n\n"
        f"{anchor}\n\n"
        f"Other names currently held in the same book (for shared-factor / "
        f"crowding context): {other_holdings_line or '(none on file)'}\n\n"
        f"{_ITEM_SCHEMA_INSTRUCTIONS}"
    )


class RedTeamLensError(RuntimeError):
    """Raised when a per-name lens call fails outright (call error) or
    produces unusable JSON on both the ``call_llm_structured`` attempts, or
    the parsed object fails schema validation. Callers distinguish transient
    vs. hard-stop failures via ``llm.cli.is_hard_stop`` on the ORIGINAL
    exception this wraps (see ``engine.py``); this class itself carries no
    hard/transient classification of its own."""


def generate_per_name_item(
    pack: NameEvidencePack,
    lens: str,
    *,
    run_key: str,
    other_holdings_line: str,
) -> RedTeamLLMItem:
    """One adversarial attack for ``pack`` under ``lens``. Raises whatever
    ``call_llm_structured`` raises (hard stops included — the caller
    classifies via ``is_hard_stop``) or ``RedTeamLensError`` when the parsed
    JSON fails ``RedTeamLLMItem`` schema validation."""
    prompt = build_prompt(pack, lens, other_holdings_line=other_holdings_line)
    try:
        payload = call_llm_structured(
            prompt,
            purpose=PURPOSE,
            ticker=pack.ticker,
            scope="red_team",
            run_id=run_key,
            expect="object",
            required_keys=("attack_md", "question_md", "proposed_change_md", "severity"),
        )
    except StructuredParseError as exc:
        raise RedTeamLensError(f"{pack.ticker}/{lens}: unusable JSON: {exc}") from exc
    try:
        return RedTeamLLMItem.model_validate(payload)
    except ValidationError as exc:
        raise RedTeamLensError(f"{pack.ticker}/{lens}: schema validation failed: {exc}") from exc

"""The seven rotating per-name attack lenses (monthly_red_team.md Phase 2).

Lens rotation is DETERMINISTIC, not stateful: ``lens_for(ticker, month_index)``
derives the lens from a stable hash of the ticker plus the calendar month
index, so no rotation-state table is needed and the choice is reproducible
from (ticker, month) alone. ``month_index`` increases by exactly 1 every
calendar month, so consecutive months always land on a different lens (the
directive's "the same name must not get the same lens twice in a row" —
guaranteed because +1 mod N is never 0 for any N > 1, where N =
``len(LENS_NAMES)``). Python's built-in ``hash()`` is process-salted
(``PYTHONHASHSEED``) and would NOT be reproducible across runs, so this uses a
fixed ``sha256``-derived seed instead.

PR9 (Bull-side symmetry) grew ``LENS_NAMES`` from 5 to 6, and tenet-2 Phase 4
grew it again from 6 to 7 (``profile_drift``) — the rotation modulus below
follows ``len(LENS_NAMES)`` automatically, so neither change needed any code
change beyond the new name in ``redteam.models``. Safe to change: ``red_team_
items`` carried zero rows in prod at both PR9 and Phase-4 time (no persisted
run has ever depended on the prior modulus's assignment), so there is no
historical rotation state to preserve continuity with.

Evidence is assembled entirely in CODE (thesis anchor, verdict, weight, latest
DCF over/under, downside/add-rung presence, affirmed-profile/expiring-fact
reads — Layer-3 discipline: no business logic riding in the prompt that code
can compute). The LLM's only job is the adversarial judgment. Two lenses
depart from the shared "attack the position" framing and get their own
dispatch + evidence shape: ``missed_upside`` (PR9) inverts direction and
attacks the owner's CAUTION instead (find where the thesis is being
under-expressed) — see ``build_missed_upside_prompt``; ``profile_drift``
(tenet-2 Phase 4) attacks the owner's PROFILE/BEHAVIORAL RECORD instead of
the thesis (is it still true, or stale/contradicted by observed behavior) —
see ``build_profile_drift_prompt``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from bear_lint import BearLintFinding
from llm.anchors import load_thesis_anchor
from llm.structured import StructuredParseError, call_llm_structured
from model_provenance.basis import dcf_basis
from position_guard import evaluate_add_trigger, evaluate_downside_trigger, fetch_intent_rows
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
    # "Bear depth" (PR9, missed_upside evidence) — the same
    # ``BearLintFinding.bear_return_pct`` (bear_fv/live_price - 1, in
    # percent) the Risk panel already reads; None under the same conditions
    # as bear_status/bear_provenance above.
    bear_return_pct: float | None = None
    # PR9 (Bull-side symmetry) evidence flags — whether THIS name has an
    # encoded downside exit rule / add-rung on file, derived via
    # ``position_guard.evaluate_downside_trigger`` /
    # ``.evaluate_add_trigger`` (the SAME detection the naked-position gate
    # uses — never re-derived). Both default False when ``conn`` isn't a
    # live ``sqlite3.Connection`` (e.g. --dry-run), matching this dataclass's
    # existing "degrade to omitted/false rather than fabricate" posture.
    has_downside_rung: bool = False
    has_add_rung: bool = False
    # Profile-drift lens (tenet-2 Phase 4) evidence — BOOK-WIDE, not
    # per-ticker: the same pack for every name assigned this lens in a given
    # run (mirrors bear_by_ticker being computed once). None when unavailable
    # (--dry-run with no connection, or the read failed) — the lens degrades
    # to attacking the empty-profile case itself rather than fabricating.
    profile_drift: ProfileDriftEvidence | None = None


@dataclass(slots=True, frozen=True)
class ProfileDriftEvidence:
    """Deterministic, book-wide evidence for the ``profile_drift`` lens
    (tenet-2 Phase 4): does the owner's AFFIRMED profile / behavioral record
    still match observed behavior? Every field is assembled from existing
    reads — ``owner_profile.store`` (affirmed facts, expiring facts) and the
    graded ``decisions`` corpus since the earliest behavioral affirmation —
    never re-derived or LLM-inferred."""

    # "key: narrative (affirmed YYYY-MM-DD)" for every currently-AFFIRMED
    # fact across all three tiers (capacity/appetite/behavioral).
    affirmed_lines: tuple[str, ...]
    # "category/key: narrative" for every fact past its review horizon
    # (owner_profile.store.list_expiring_facts) — the direct, deterministic
    # "stale" signal §3.3 defines.
    expiring_lines: tuple[str, ...]
    # One honest sentence on whether GRADED owner decisions since the
    # earliest behavioral-fact affirmation still confirm the pattern, or
    # None when there is nothing behavioral affirmed yet to check against.
    graded_since_summary: str | None


def build_profile_drift_evidence(conn: object) -> ProfileDriftEvidence | None:
    """Assemble :class:`ProfileDriftEvidence` over an open connection.
    ``None`` when ``conn`` isn't a live ``sqlite3.Connection`` (e.g.
    --dry-run) or the read fails for any reason (missing pre-0159 substrate,
    locked DB) — best-effort, never blocks the per-name pass."""
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        return None
    try:
        from owner_profile.store import get_current_profile, list_expiring_facts

        grouped = get_current_profile(conn)
        expiring = list_expiring_facts(conn)
    except Exception as exc:  # best-effort — never blocks the pass
        log.debug({"event": "red_team_profile_drift_evidence_failed", "error": str(exc)})
        return None

    affirmed_rows = [row for rows in grouped.values() for row in rows]
    affirmed_lines = tuple(
        f"{row.key}: {row.narrative} (affirmed {(row.affirmed_at or row.created_at)[:10]})"
        for row in sorted(affirmed_rows, key=lambda r: r.id)
    )
    expiring_lines = tuple(
        f"{row.category}/{row.key}: {row.narrative}" for row in sorted(expiring, key=lambda r: r.id)
    )

    behavioral_affirmed = [r for r in grouped.get("behavioral", []) if r.affirmed_at]
    graded_since_summary: str | None = None
    if behavioral_affirmed:
        earliest = min(r.affirmed_at for r in behavioral_affirmed if r.affirmed_at)
        try:
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN outcome_label = 'wrong' THEN 1 ELSE 0 END) "
                "FROM decisions WHERE decided_by = 'owner' AND outcome_label != 'pending' "
                "AND made_at >= ?",
                (earliest,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None and row[0]:
            total, wrong = int(row[0]), int(row[1] or 0)
            graded_since_summary = (
                f"{wrong} of {total} owner decisions graded since the earliest behavioral "
                f"affirmation ({earliest[:10]}) came back wrong"
            )

    return ProfileDriftEvidence(
        affirmed_lines=affirmed_lines,
        expiring_lines=expiring_lines,
        graded_since_summary=graded_since_summary,
    )


def build_name_evidence_pack(
    repo_root: Path,
    conn: object,
    *,
    ticker: str,
    weight_pct: float,
    bear_finding: BearLintFinding | None = None,
    profile_drift_evidence: ProfileDriftEvidence | None = None,
) -> NameEvidencePack | None:
    """Assemble one held name's evidence pack. ``None`` when there is no
    holdings JSON on file at all (nothing to attack — the caller skips and
    tallies it, it never fabricates a thesis). ``bear_finding`` is the
    caller's pre-computed ``bear_lint.build_bear_lint`` row for this ticker
    (one book-wide lint pass, not one per name) — omitted when the caller has
    none (a DB without ``dcf_runs``, or bear_lint unavailable).
    ``profile_drift_evidence`` is likewise book-wide (computed ONCE per run,
    not per name) — see :func:`build_profile_drift_evidence`."""
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

    # PR9 evidence flags: reuse position_guard's exact detection (never
    # re-derived) via the ticker's position_sizing_intent history over the
    # SAME open connection. Best-effort — a read failure degrades to False
    # (no rung on file), never blocks the pack.
    has_downside_rung = False
    has_add_rung = False
    if isinstance(conn, sqlite3.Connection):
        try:
            intents = fetch_intent_rows(conn, ticker)
            has_downside_rung = evaluate_downside_trigger(
                intents, repo_root=repo_root, ticker=ticker
            ).passed
            has_add_rung = evaluate_add_trigger(intents).passed
        except Exception as exc:  # best-effort — never blocks the pack
            log.debug(
                {"event": "red_team_rung_evidence_failed", "ticker": ticker, "error": str(exc)}
            )

    return NameEvidencePack(
        ticker=ticker.upper(),
        weight_pct=weight_pct,
        thesis_anchor_md=thesis_anchor_md,
        verdict=(verdict if isinstance(verdict, str) else None),
        key_driver=(key_driver if isinstance(key_driver, str) else None),
        over_under_pct=over_under_pct,
        bear_status=(bear_finding.status if bear_finding is not None else None),
        bear_provenance=(bear_finding.provenance if bear_finding is not None else None),
        bear_return_pct=(bear_finding.bear_return_pct if bear_finding is not None else None),
        has_downside_rung=has_downside_rung,
        has_add_rung=has_add_rung,
        profile_drift=profile_drift_evidence,
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
        'book. State the factor explicitly and falsifiably — not "the '
        'economy" but a specific, checkable driver.'
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


@dataclass(slots=True, frozen=True)
class _EvidenceLines:
    """The evidence sentences shared by every lens prompt — assembled once so
    ``build_prompt`` and ``build_missed_upside_prompt`` render the identical
    facts, never two independently-drifting phrasings of the same pack."""

    verdict: str
    driver: str
    weight: str
    dcf: str
    bear: str
    anchor: str


def _evidence_lines(pack: NameEvidencePack) -> _EvidenceLines:
    dcf_line = (
        f"DCF model disagrees with the market by {pack.over_under_pct:+.1%} "
        "(fair value vs. live price)."
        if pack.over_under_pct is not None
        else "No DCF fair-value model on file for this name."
    )
    bear_line = ""
    if pack.bear_status is not None:
        prov = f", provenance {pack.bear_provenance}" if pack.bear_provenance else ""
        depth = f", bear case {pack.bear_return_pct:+.1%} vs live" if pack.bear_return_pct else ""
        bear_line = f" Bear-realism lint: {pack.bear_status}{prov}{depth}."
    return _EvidenceLines(
        verdict=(f"Current verdict: {pack.verdict}." if pack.verdict else ""),
        driver=(f"Key driver: {pack.key_driver}." if pack.key_driver else ""),
        weight=f"Book weight: {pack.weight_pct:.1%} of portfolio.",
        dcf=dcf_line,
        bear=bear_line,
        anchor=pack.thesis_anchor_md.strip() or "(no thesis anchor on file)",
    )


def build_prompt(pack: NameEvidencePack, lens: str, *, other_holdings_line: str) -> str:
    """Compose the one-shot adversarial prompt for ``lens`` over ``pack``.
    Dispatches to :func:`build_missed_upside_prompt` (``missed_upside``, PR9)
    and :func:`build_profile_drift_prompt` (``profile_drift``, tenet-2 Phase
    4) — the two lenses whose evidence shape and attack target depart from
    the shared position-attack framing below."""
    if lens == "missed_upside":
        return build_missed_upside_prompt(pack, other_holdings_line=other_holdings_line)
    if lens == "profile_drift":
        return build_profile_drift_prompt(pack, other_holdings_line=other_holdings_line)
    framing = _LENS_FRAMING.get(lens, _LENS_FRAMING["shared_factor"])
    ev = _evidence_lines(pack)

    return (
        f"You are the monthly adversarial Red Team reviewing a held position, "
        f"{pack.ticker}, in a long-only equity portfolio. Your job is NOT to "
        f"summarize the thesis — it is to attack it.\n\n"
        f"{framing}\n\n"
        f"{ev.weight} {ev.verdict} {ev.driver} {ev.dcf}{ev.bear}\n\n"
        f"{ev.anchor}\n\n"
        f"Other names currently held in the same book (for shared-factor / "
        f"crowding context): {other_holdings_line or '(none on file)'}\n\n"
        f"{_ITEM_SCHEMA_INSTRUCTIONS}"
    )


def build_missed_upside_prompt(pack: NameEvidencePack, *, other_holdings_line: str) -> str:
    """The ``missed_upside`` lens (Bull-side symmetry, PR9): attacks the
    owner's CAUTION on ``pack.ticker``, not the position — the mirror image
    of every other lens above. See the module docstring for why this exists
    (the program's other five lenses all attack longs; nothing attacked the
    owner's tendency to under-underwrite upside)."""
    ev = _evidence_lines(pack)
    rung_line = (
        f"Downside rung encoded: {'yes' if pack.has_downside_rung else 'no'}. "
        f"Add-rung (buy pre-commitment) encoded: {'yes' if pack.has_add_rung else 'no'}."
    )

    return (
        f"You are the monthly adversarial Red Team reviewing a held position, "
        f"{pack.ticker}, in a long-only equity portfolio. You are attacking "
        f"the owner's caution on {pack.ticker}, not the position — the "
        "opposite job of the other five lenses.\n\n"
        f"{ev.weight} {ev.verdict} {ev.driver} {ev.dcf}{ev.bear} {rung_line}\n\n"
        f"{ev.anchor}\n\n"
        "Using the evidence pack above (thesis + verdict, weight, DCF "
        "over/under, bear provenance and bear depth, encoded downside rungs, "
        "encoded add-rungs or their absence), produce ONE falsifiable attack "
        "on under-underwritten upside: a position trimmed or capped below "
        "the owner's own fair value; a high-conviction name with no encoded "
        "add-rung (sell rules but no buy rules); a bear delta materially "
        "more severe than the encoded thesis breakers justify; or "
        "conviction claimed in the thesis that current weight does not "
        "express. Propose a concrete bull-side pre-commitment: an add-rung "
        "(price level + size + thesis-intact condition) or a bear-delta "
        "revision.\n\n"
        f"Other names currently held in the same book (for context): "
        f"{other_holdings_line or '(none on file)'}\n\n"
        f"{_ITEM_SCHEMA_INSTRUCTIONS}"
    )


def build_profile_drift_prompt(pack: NameEvidencePack, *, other_holdings_line: str) -> str:
    """The ``profile_drift`` lens (tenet-2 Phase 4, docs/design/
    tenet2_advisory_program.md §3.3): attacks whether the owner's AFFIRMED
    profile facts and behavioral rules still match observed behavior, not
    ``pack.ticker``'s thesis — a third departure from the shared
    position-attack framing (``pack.ticker`` anchors the attack in a concrete
    name's context, same as every other lens, but the claim under attack is
    about the OWNER, not the position).

    Zero evidence (no affirmed facts, no expiring facts, no graded record
    since affirmation — ``pack.profile_drift`` is ``None`` or entirely empty)
    pivots the attack to the empty-profile case itself: the platform has run
    for months on graded decisions with NOTHING ratified, which is its own
    falsifiable drift claim, never fabricated evidence."""
    ev = _evidence_lines(pack)
    pd = pack.profile_drift
    if pd is None or not (pd.affirmed_lines or pd.expiring_lines or pd.graded_since_summary):
        profile_block = (
            "No owner_profile_facts are currently AFFIRMED (whether because none has "
            "ever been ratified, or the read failed) despite graded decisions "
            "accumulating in the platform's ledger."
        )
    else:
        lines = ["Currently AFFIRMED owner profile / behavioral facts:"]
        if pd.affirmed_lines:
            lines.extend(f"- {line}" for line in pd.affirmed_lines)
        else:
            lines.append("- (none affirmed)")
        if pd.expiring_lines:
            lines.append(
                "Facts already past their review horizon (stale by the platform's own rule):"
            )
            lines.extend(f"- {line}" for line in pd.expiring_lines)
        if pd.graded_since_summary:
            lines.append(f"Graded record since affirmation: {pd.graded_since_summary}.")
        profile_block = "\n".join(lines)

    return (
        f"You are the monthly adversarial Red Team reviewing a long-only equity "
        f"book. You are attacking the OWNER'S PROFILE AND BEHAVIORAL RECORD, not "
        f"{pack.ticker}'s thesis — the opposite target of every other lens, which "
        f"attacks the position itself.\n\n"
        f"{profile_block}\n\n"
        f"For grounding context, one held position: {pack.ticker}. {ev.weight} "
        f"{ev.verdict} {ev.driver}\n\n"
        "ATTACK ANGLE: identify the SINGLE most dangerous way the owner's affirmed "
        "profile/behavioral facts are stale or contradicted by observed behavior — "
        "an expiring fact nobody has re-ratified, a behavioral rule whose graded "
        "record since affirmation no longer confirms it, or (if nothing is "
        "affirmed at all) the empty-profile gap itself. Propose a concrete "
        "re-affirmation or supersede action the owner should take.\n\n"
        f"Other names currently held in the same book (for context): "
        f"{other_holdings_line or '(none on file)'}\n\n"
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
            schema=TypeAdapter(RedTeamLLMItem),
        )
    except StructuredParseError as exc:
        raise RedTeamLensError(f"{pack.ticker}/{lens}: unusable JSON: {exc}") from exc
    try:
        return RedTeamLLMItem.model_validate(payload)
    except ValidationError as exc:
        raise RedTeamLensError(f"{pack.ticker}/{lens}: schema validation failed: {exc}") from exc

"""Encode a coach conversation into a proposed PositioningProfile.

The bridge from semantics to state: after the coach conversation converges,
one governed structured call (``positioning_encode``) reads the thread and
emits a :class:`~positioning.profile.PositioningProfile` PROPOSAL. Nothing is
persisted here — the proposal renders as an editable approval form
(scenario_prior owner-wins precedent) and only ``/api/positioning/approve``,
re-validated FROM THE FORM VALUES, writes the intent. So the LLM proposes,
the owner disposes, and a bad encode can never silently become the target
book.

Failure is loud: an encode whose ``profile`` doesn't validate (out-of-range
tilt, unknown sleeve, non-taxonomy sector) raises — surfaced as an error in
the UI, never a silently-empty proposal.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from ask.store import load_recent_history
from positioning.profile import SLEEVE_KEYS, PositioningProfile
from positioning.store import latest_intent
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# NOTE: ``llm.structured`` is imported function-locally in ``propose_profile``
# (the only caller), NOT here. A module-level import drags the whole llm
# transport chain — llm.cli → llm.ledger → llm.fallback → google.genai,
# ~10s cold — into every importer of :class:`ProposedProfile`, and
# ``pipeline.positioning_panel``'s render path never makes an LLM call.

ENCODE_PURPOSE = "positioning_encode"

#: How much conversation the encoder reads. Long enough to cover a full
#: back-and-forth; the coach restates converged targets near the end anyway.
_MAX_TURNS = 30
_MAX_CHARS = 2000


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """One dimension of the proposal vs the currently-active profile."""

    fieldname: str
    proposed: str
    active: str  # '(unset)' when the active profile doesn't express it


@dataclass(frozen=True, slots=True)
class ProposedProfile:
    """The encode output the approval form renders."""

    profile: PositioningProfile
    summary: str  # 2-4 sentence narrative to persist on approval
    rationale: str  # why these numbers, quoted from the conversation
    diffs: list[FieldDiff] = field(default_factory=list[FieldDiff])
    session_id: str = ""


class EncodeError(RuntimeError):
    """The conversation could not be encoded into a valid profile."""


class _EncodeOutput(BaseModel):
    profile: PositioningProfile
    summary: str = Field(min_length=1)
    rationale: str = ""


_ENCODE_ADAPTER = TypeAdapter(_EncodeOutput)


def _prompt(
    thread_text: str, sector_vocabulary: list[str], active: PositioningProfile | None
) -> str:
    active_block = (
        f"CURRENTLY ACTIVE PROFILE (the baseline being revised):\n{active.model_dump_json(indent=2)}"
        if active is not None
        else "CURRENTLY ACTIVE PROFILE: none — this proposal creates the first one."
    )
    vocab = ", ".join(sector_vocabulary) if sector_vocabulary else "(unavailable)"
    return f"""Encode the owner's portfolio-positioning conversation below into a
quantitative target profile.

HARD RULES:
- Emit ONLY dimensions the owner actually expressed or explicitly agreed to
  in the conversation. Leave everything else null / empty — an unexpressed
  dimension must NOT be invented.
- Sector labels must come from this closed vocabulary, verbatim: {vocab}
- Sleeve keys are limited to: {", ".join(sorted(SLEEVE_KEYS))}
- target_growth_tilt speaks QQQ-beta-minus-SPY-beta units, in [-2, 2].
- Weights and sleeve targets are decimal fractions of the book in [0, 1];
  sector target_weight values must sum to at most 1.0.
- vol_posture is one of reduce | hold | increase, only if the owner said so.
- life_circumstances: copy the owner's own words (short phrases), verbatim.
- summary: 2-4 sentences restating the positioning in plain language — this
  is persisted as the profile's narrative.
- rationale: 1-3 sentences citing WHERE in the conversation each number came
  from.

{active_block}

CONVERSATION:
{thread_text}

Respond with ONE JSON object:
{{
  "profile": {{
    "target_growth_tilt": <float|null>,
    "growth_tilt_band": <float, default 0.15>,
    "vol_posture": <"reduce"|"hold"|"increase"|null>,
    "target_vol_ann": <float|null>,
    "sharpe_floor": <float|null>,
    "sector_targets": [{{"sector": "<vocabulary label>", "target_weight": <float>, "band": <float>}}],
    "sleeves": {{"<sleeve key>": <float>}},
    "max_position_weight": <float|null>,
    "horizon_years": <float|null>,
    "life_circumstances": ["<owner's words>"]
  }},
  "summary": "<2-4 sentences>",
  "rationale": "<1-3 sentences>"
}}"""


def _field_diffs(
    proposed: PositioningProfile, active: PositioningProfile | None
) -> list[FieldDiff]:
    diffs: list[FieldDiff] = []
    prop = proposed.model_dump()
    act = active.model_dump() if active is not None else {}
    for key in prop:
        p_val, a_val = prop[key], act.get(key)
        if p_val in (None, [], {}) and a_val in (None, [], {}):
            continue
        p_s = json.dumps(p_val, default=str) if p_val not in (None, [], {}) else "(unset)"
        a_s = json.dumps(a_val, default=str) if a_val not in (None, [], {}) else "(unset)"
        if p_s != a_s:
            diffs.append(FieldDiff(fieldname=key, proposed=p_s, active=a_s))
    return diffs


def propose_profile(
    db_path: Path,
    repo_root: Path,
    *,
    session_id: str,
    sector_vocabulary: list[str] | None = None,
) -> ProposedProfile:
    """One structured encode of the coach thread → an owner-editable proposal.

    Raises :class:`EncodeError` when the thread is empty or the model's
    ``profile`` fails validation (loud, per the boundary contract) — and lets
    ``call_llm_structured``'s own hard-stop/parse errors propagate untouched.
    """
    from llm.structured import call_llm_structured  # lazy — see module NOTE

    history = load_recent_history(
        session_id, db_path=db_path, max_turns=_MAX_TURNS, max_chars=_MAX_CHARS
    )
    if not history:
        raise EncodeError("no conversation turns on this session yet — talk to the coach first")
    thread_text = "\n\n".join(f"[{h['role'].upper()}] {h['text']}" for h in history)

    if sector_vocabulary is None:
        from candidate_fit_cache import assemble_book_context

        book = assemble_book_context(repo_root, db_path=db_path)
        sector_vocabulary = sorted(book.sector_weights)

    active: PositioningProfile | None = None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            row = latest_intent(conn)
            active = row.profile if row is not None else None
        finally:
            conn.close()

    payload = call_llm_structured(
        _prompt(thread_text, sector_vocabulary, active),
        purpose=ENCODE_PURPOSE,
        scope="positioning",
        expect="object",
        required_keys=("profile", "summary"),
        schema=_ENCODE_ADAPTER,
        db_path=db_path,
    )
    try:
        rec = _EncodeOutput.model_validate(payload)
        profile = rec.profile
    except ValueError as exc:
        raise EncodeError(f"encoded profile failed validation: {exc}") from exc
    if profile.is_empty():
        raise EncodeError(
            "the conversation hasn't expressed any quantitative target yet — "
            "iterate with the coach until at least one dimension is concrete"
        )
    summary = rec.summary.strip()
    if not summary:
        raise EncodeError("encode returned an empty summary")
    return ProposedProfile(
        profile=profile,
        summary=summary,
        rationale=rec.rationale.strip(),
        diffs=_field_diffs(profile, active),
        session_id=session_id,
    )

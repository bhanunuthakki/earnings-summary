"""Weighted, recency-decayed discovery scoring — the Discovery rule's engine.

This replaces ``run_discovery``'s equal-weight integer count (one point per
screen passed, one per adjacency source) with a weighted sum of typed, dated
signals scored through the ``discovery_sources`` weight registry — the
Instrument Paradigm's "ranked surface scores by a weighted sum of typed, dated
signals through a source-weight registry, never an equal-weight count, and
carries its ``score_why``".

The model (the Discovery rule, design_language §"Discovery"):

* **Per-signal contribution** = ``weight × raw_strength × action_mult × decay``.
  ``weight`` is the source's editable ``base_weight``; ``raw_strength`` is the
  signal-native magnitude normalized toward ~1.0 (a screen pass = 1.0, an
  adjacency = capped mention count, a 13F move = position size vs the fund's
  book); ``decay`` is a per-class recency half-life.
* **Action asymmetry** (13F only): a NEW initiation ≫ an incremental ADD, and
  the gap is WIDER for low-turnover long-only funds (a new Edgewood/Loomis
  position is loud; their adds are routine rebalancing) than for higher-
  frequency hedge funds (whose adds are themselves momentum signals).
* **Corroboration**: 2+ distinct funds holding the same name is a super-linear
  signal — the investor term is multiplied by ``n_funds ** CORROB_EXP``.
* **Clamp**: a name surfaced ONLY by investor research (no screen/adjacency
  corroboration) can SURFACE but cannot top the queue on investor weight alone
  — its investor term is capped below any fundamentally-corroborated name.
* **Entry threshold**: a candidate must clear ``ENTRY_THRESHOLD`` to enter the
  queue at all (the owner's "raise the entry bar so weak names never enter"
  decision; the panel separately caps the RENDER to a ranked top-N).

Pure and deterministic: no DB, no clock except an injectable ``now``. The store
resolves roster weights into ``Signal``s; this module scores them and emits the
``score_why`` breakdown persisted in ``discovery_candidates.score_json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import cast

# --- the Discovery rule's calibration knobs (one place; the guard tests pin
#     that moving a weight moves the rank, not these exact values) ----------

#: A candidate must clear this weighted score to be upserted into the queue.
#: One clean screen (weight ~1.0, fresh) ≈ 1.0 and does NOT clear alone — it
#: takes a second corroborating signal (another screen, an adjacency mention,
#: or an investor move) to enter. Weak singletons never reach the owner.
ENTRY_THRESHOLD: float = 1.5

#: Recency half-lives (days) per signal class. Screens/adjacency re-derive from
#: current data every run, so they barely decay; a 13F carries a 45-day filing
#: lag and a position is stale within ~2 quarters.
HALF_LIFE_DAYS: dict[str, float] = {
    "screen": 365.0,
    "adjacency": 240.0,
    "investor_13f": 120.0,
}
_DEFAULT_HALF_LIFE: float = 240.0

#: Action multipliers by fund style. Low-turnover long-only houses: a NEW
#: position is the loud signal, an ADD is near-noise. Higher-frequency funds:
#: an ADD still carries momentum signal. trim/exit are not discovery signals.
_ACTION_MULT_LOW_TURNOVER: dict[str, float] = {"new": 1.0, "add": 0.25, "trim": 0.0, "exit": 0.0}
_ACTION_MULT_DEFAULT: dict[str, float] = {"new": 1.0, "add": 0.55, "trim": 0.0, "exit": 0.0}

#: Super-linear corroboration exponent: the investor term is scaled by
#: ``n_funds ** CORROB_EXP`` (n=1 → 1.0, n=2 → 1.32, n=3 → 1.55, n=4 → 1.74).
#: Since the summed term already grows ~linearly in n, the product is
#: super-linear in the number of corroborating funds.
CORROB_EXP: float = 0.4

#: The ceiling on the investor term for a name with NO screen/adjacency
#: corroboration — it can SURFACE (the cap sits above ``ENTRY_THRESHOLD`` so a
#: corroborated investor-only name enters the queue) but cannot TOP it (the cap
#: sits below a fundamentally-corroborated name, whose investor term is
#: uncapped). A single fund's move stays below the entry bar; 2+ funds clear it.
INVESTOR_ONLY_CAP: float = 2.0

#: Adjacency strength caps at this many occurrences (distinct holdings naming
#: it / capped transcript mentions) so a name on three watchlists doesn't
#: outscore the fundamentals.
ADJ_OCCURRENCE_CAP: float = 3.0

#: A 13F position this size (fraction of the fund's reported book) = strength
#: 1.0; bigger positions scale up to ``INVESTOR_STRENGTH_CAP``.
INVESTOR_PCT_REF: float = 0.02
INVESTOR_STRENGTH_CAP: float = 3.0

_INVESTOR_CLASS = "investor_13f"


@dataclass(frozen=True, slots=True)
class Signal:
    """One typed, dated, weighted discovery signal — the unit the scorer sums.

    ``weight`` is the source's resolved ``base_weight``; ``raw_strength`` the
    signal-native magnitude (normalized toward ~1.0 by the constructors below);
    ``observed_at`` the EVENT time the recency decay reads. ``action`` and
    ``style_tags`` apply only to ``investor_13f`` (the new-vs-add asymmetry)."""

    signal_class: str
    source_key: str
    weight: float
    raw_strength: float
    observed_at: datetime
    detail: str = ""
    action: str | None = None
    style_tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """A scored candidate: the final ``score`` and the ``why`` breakdown that
    becomes ``discovery_candidates.score_json``."""

    score: float
    why: dict[str, object]

    @property
    def passes_entry(self) -> bool:
        return passes_entry_threshold(self.score)


# ---------------------------------------------------------------------------
# Signal constructors (the weight-aware adapters from raw hits)
# ---------------------------------------------------------------------------


def _naive_utc(value: date | datetime) -> datetime:
    """Coerce a date/datetime to a naive-UTC datetime (the repo's stored form)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    return datetime(value.year, value.month, value.day)


def screen_signal(
    source_key: str, *, weight: float, observed_at: date | datetime, detail: str = ""
) -> Signal:
    """A factor-screen pass — binary strength 1.0, scaled only by the screen's
    roster weight."""
    return Signal("screen", source_key, weight, 1.0, _naive_utc(observed_at), detail)


def adjacency_signal(
    source_key: str,
    *,
    weight: float,
    observed_at: date | datetime,
    occurrences: float = 1.0,
    detail: str = "",
) -> Signal:
    """An adjacency mention — strength is the capped occurrence count (distinct
    holdings naming it / capped transcript mentions), scaled by the channel's
    roster weight (watchlist > transcript > news)."""
    strength = min(max(float(occurrences), 1.0), ADJ_OCCURRENCE_CAP)
    return Signal("adjacency", source_key, weight, strength, _naive_utc(observed_at), detail)


def investor_signal(
    source_key: str,
    *,
    weight: float,
    observed_at: date | datetime,
    pct_of_book: float,
    action: str,
    style_tags: tuple[str, ...] | list[str] = (),
    detail: str = "",
) -> Signal:
    """A 13F move — strength is the position size as a fraction of the fund's
    reported book (a 2% position = 1.0), carrying the ``action`` and
    ``style_tags`` the asymmetry reads."""
    strength = min(max(float(pct_of_book), 0.0) / INVESTOR_PCT_REF, INVESTOR_STRENGTH_CAP)
    return Signal(
        _INVESTOR_CLASS,
        source_key,
        weight,
        strength,
        _naive_utc(observed_at),
        detail,
        action,
        tuple(style_tags),
    )


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------


def recency_decay(observed_at: date | datetime, now: datetime, signal_class: str) -> float:
    """Half-life decay: ``0.5 ** (age_days / half_life)``, clamped so a future
    or same-day stamp is 1.0."""
    half_life = HALF_LIFE_DAYS.get(signal_class, _DEFAULT_HALF_LIFE)
    age_days = max((now - _naive_utc(observed_at)).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / half_life)


def _action_multiplier(action: str | None, style_tags: tuple[str, ...]) -> float:
    if action is None:
        return 1.0
    table = _ACTION_MULT_LOW_TURNOVER if "low_turnover" in style_tags else _ACTION_MULT_DEFAULT
    return table.get(action, 0.0)


def passes_entry_threshold(score: float) -> bool:
    """Whether a weighted score clears the entry bar (the owner's "raise the
    entry threshold so weak names never enter the queue")."""
    return score >= ENTRY_THRESHOLD


def score_candidate(signals: list[Signal], *, now: datetime | None = None) -> ScoreResult:
    """Score one candidate from its signals; emit the ``score_why`` breakdown.

    Splits into a fundamental term (screens + adjacency) and an investor term
    (13F, corroboration-boosted then clamped when no fundamental corroboration
    exists), so an investor-only name surfaces but cannot dominate."""
    now = now or datetime.now(UTC).replace(tzinfo=None)

    per_signal: list[dict[str, object]] = []
    class_terms: dict[str, float] = {}
    investor_contribs: list[tuple[str, float]] = []

    for sig in signals:
        decay = recency_decay(sig.observed_at, now, sig.signal_class)
        amult = _action_multiplier(sig.action, sig.style_tags)
        contribution = sig.weight * sig.raw_strength * amult * decay
        per_signal.append(
            {
                "class": sig.signal_class,
                "source_key": sig.source_key,
                "weight": round(sig.weight, 4),
                "strength": round(sig.raw_strength, 4),
                "action": sig.action,
                "decay": round(decay, 4),
                "contribution": round(contribution, 4),
                "detail": sig.detail,
            }
        )
        if sig.signal_class == _INVESTOR_CLASS:
            investor_contribs.append((sig.source_key, contribution))
        else:
            class_terms[sig.signal_class] = class_terms.get(sig.signal_class, 0.0) + contribution

    fundamental = sum(class_terms.values())

    investor_raw = sum(c for _, c in investor_contribs)
    n_funds = len({k for k, c in investor_contribs if c > 0})
    corrob_mult = float(n_funds) ** CORROB_EXP if n_funds >= 1 else 1.0
    investor_term = investor_raw * corrob_mult

    clamped = False
    if fundamental <= 0.0 and investor_term > INVESTOR_ONLY_CAP:
        investor_term = INVESTOR_ONLY_CAP
        clamped = True

    total = fundamental + investor_term

    terms = {cls: round(val, 4) for cls, val in sorted(class_terms.items())}
    if investor_contribs:
        terms[_INVESTOR_CLASS] = round(investor_term, 4)

    why: dict[str, object] = {
        "v": 1,
        "total": round(total, 4),
        "terms": terms,
        "fundamental": round(fundamental, 4),
        "investor": round(investor_term, 4),
        "investor_raw": round(investor_raw, 4),
        "clamped": clamped,
        "corroboration": {"n_funds": n_funds, "multiplier": round(corrob_mult, 4)},
        "entry_threshold": ENTRY_THRESHOLD,
        "signals": per_signal,
        "scored_at": now.isoformat(),
    }
    return ScoreResult(score=round(total, 4), why=why)


def score_evidence_line(why: dict[str, object]) -> str:
    """A one-line, human evidence summary from a ``score_why`` dict — the
    inline row the panel shows before the full breakdown peek. Deterministic;
    safe to render with ``escape()``."""
    terms_raw = why.get("terms")
    parts: list[str] = []
    if isinstance(terms_raw, dict):
        terms = cast("dict[str, object]", terms_raw)
        labels = {"screen": "screens", "adjacency": "adjacency", "investor_13f": "funds"}
        for cls, label in labels.items():
            val = terms.get(cls)
            if isinstance(val, (int, float)) and val > 0:
                parts.append(f"{label} {float(val):.1f}")
    corr_raw = why.get("corroboration")
    if isinstance(corr_raw, dict):
        corr = cast("dict[str, object]", corr_raw)
        n = corr.get("n_funds")
        mult = corr.get("multiplier")
        if isinstance(n, int) and n >= 2 and isinstance(mult, (int, float)):
            parts.append(f"{n} funds x{float(mult):.2f}")
    if why.get("clamped"):
        parts.append("investor-only (capped)")
    total = why.get("total")
    head = f"{float(total):.1f}" if isinstance(total, (int, float)) else "?"
    return f"score {head} — " + " · ".join(parts) if parts else f"score {head}"

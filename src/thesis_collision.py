"""Thesis-collision detection — the whole-book cross-name audit.

Every other risk section in this build treats holdings as independent legs
(a beta, a correlation, a bear return). Nothing has read what each name's
thesis actually *depends on* and checked whether the book is more
concentrated — or more internally contradictory — than the position-level
numbers show. Two names can look diversified on price correlation while
both quietly betting on the same underlying driver (e.g. "Brazilian
consumer credit normalizes"); or two theses can make opposite bets on the
same macro view without either analyst noticing.

This is a governed LLM purpose following the **operational recipe**
(``LLM_MODELS`` + ``prompt_versions`` only — no eval-golden lockstep; see
[[reference_operational_llm_purpose_recipe]]): one call over the whole
portfolio's thesis narratives + tier-1 break-rule drivers (read straight off
``micro_thesis/holdings/<T>.json`` — no DB, no ``financial_facts`` scan),
flagging **shared-driver clusters** (independent-looking names that are
really one bet) and **contradictions** (theses that can't both be right).
``propose_thesis_collisions`` raises ``StructuredParseError`` on unusable
JSON (the caller degrades it); every finding is validated IN CODE against
the actual ticker set before it can render — a hallucinated ticker or a
single-name "cluster" is dropped, never trusted from the model.

Caching: :func:`refresh_thesis_collision` is the sole write path, keyed by a
thesis-set hash (``llm_artifact_store``'s ``cache_inputs``, scope
``"portfolio"``) — a call is skipped entirely when no thesis/driver text
changed since the last run. The Risk-tab section only ever reads the cached
artifact back (:func:`read_cached_report`), so this purpose never touches the
render path.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)

PURPOSE = "thesis_collision"
_SCOPE = "portfolio"

# Truncation caps so one bloated thesis narrative can't blow up the whole-book
# prompt; a book of ~10-15 names easily fits within these.
THESIS_CHAR_CAP = 600
DRIVER_CHAR_CAP = 160
MAX_TIER1_PER_TICKER = 4
_RATIONALE_CAP = 300

_HOLDINGS_DIRNAME = ("micro_thesis", "holdings")


@dataclass(frozen=True, slots=True)
class ThesisSnapshot:
    """One name's condensed thesis + tier-1 break-rule drivers, as read
    straight off its holdings JSON (no DB, no anchor composition)."""

    ticker: str
    key_driver: str
    thesis: str
    tier1_breaks: tuple[tuple[str, str], ...]  # (kpi name, break_condition)


def _load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    path = repo_root.joinpath(*_HOLDINGS_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _tier1_breaks(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
    raw = payload.get("tier_1_kpis")
    if not isinstance(raw, list):
        return ()
    out: list[tuple[str, str]] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict) or len(out) >= MAX_TIER1_PER_TICKER:
            continue
        e = cast("dict[str, object]", entry)
        name = e.get("name")
        bc = e.get("break_condition")
        if not isinstance(name, str) or not name.strip():
            continue
        bc_text = bc.strip()[:DRIVER_CHAR_CAP] if isinstance(bc, str) and bc.strip() else "—"
        out.append((name.strip(), bc_text))
    return tuple(out)


def load_thesis_snapshot(repo_root: Path, ticker: str) -> ThesisSnapshot | None:
    """One name's snapshot, or ``None`` when there's no holdings file / no
    usable thesis text (nothing to compare for this name)."""
    payload = _load_holdings_json(repo_root, ticker)
    if payload is None:
        return None
    thesis_raw = payload.get("thesis")
    thesis = thesis_raw.strip() if isinstance(thesis_raw, str) else ""
    if not thesis:
        return None
    driver_raw = payload.get("key_driver")
    driver = driver_raw.strip() if isinstance(driver_raw, str) else ""
    return ThesisSnapshot(
        ticker=ticker.upper(),
        key_driver=driver[:DRIVER_CHAR_CAP],
        thesis=thesis[:THESIS_CHAR_CAP],
        tier1_breaks=_tier1_breaks(payload),
    )


def load_thesis_snapshots(repo_root: Path, tickers: Sequence[str]) -> list[ThesisSnapshot]:
    """Snapshots for every ticker that has one, sorted by ticker. Tickers with
    no holdings file or empty thesis are silently omitted (nothing to say)."""
    out = [load_thesis_snapshot(repo_root, t) for t in {t.upper() for t in tickers}]
    return sorted((s for s in out if s is not None), key=lambda s: s.ticker)


def cache_inputs(snapshots: Sequence[ThesisSnapshot]) -> list[bytes | str]:
    """The thesis-set fingerprint fed to ``llm_artifact_store``'s cache-key
    hash — one deterministic string per snapshot (already ticker-sorted), so
    the hash changes iff any name's thesis/driver/breaks text changes.
    Typed ``list[bytes | str]`` to match ``compute_input_sha256`` directly
    (list invariance means a plain ``list[str]`` doesn't satisfy it)."""
    out: list[bytes | str] = [
        f"{s.ticker}|{s.key_driver}|{s.thesis}|" + ";".join(f"{n}::{c}" for n, c in s.tier1_breaks)
        for s in snapshots
    ]
    return out


# ---------------------------------------------------------------------------
# Structured findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SharedDriverCluster:
    """Two or more names whose theses really depend on the same driver."""

    tickers: tuple[str, ...]
    driver: str
    rationale: str


@dataclass(frozen=True, slots=True)
class ThesisContradiction:
    """Two names whose theses make incompatible bets."""

    tickers: tuple[str, ...]
    contradiction: str
    rationale: str


@dataclass(frozen=True, slots=True)
class CollisionReport:
    """The validated findings for one run over ``tickers_analyzed``."""

    clusters: tuple[SharedDriverCluster, ...] = ()
    contradictions: tuple[ThesisContradiction, ...] = ()
    tickers_analyzed: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "clusters": [
                {"tickers": list(c.tickers), "driver": c.driver, "rationale": c.rationale}
                for c in self.clusters
            ],
            "contradictions": [
                {
                    "tickers": list(c.tickers),
                    "contradiction": c.contradiction,
                    "rationale": c.rationale,
                }
                for c in self.contradictions
            ],
            "tickers_analyzed": list(self.tickers_analyzed),
        }

    @staticmethod
    def from_json(payload: object) -> CollisionReport | None:
        """Parse a previously-persisted ``content_json`` blob. ``None`` on any
        shape mismatch (a corrupted/foreign cache row degrades to empty)."""
        if not isinstance(payload, dict):
            return None
        p = cast("dict[str, object]", payload)
        analyzed = tuple(
            t for t in cast("list[object]", p.get("tickers_analyzed") or []) if isinstance(t, str)
        )
        clusters: list[SharedDriverCluster] = []
        for raw in cast("list[object]", p.get("clusters") or []):
            if not isinstance(raw, dict):
                continue
            r = cast("dict[str, object]", raw)
            tickers = tuple(
                t for t in cast("list[object]", r.get("tickers") or []) if isinstance(t, str)
            )
            driver, rationale = r.get("driver"), r.get("rationale")
            if tickers and isinstance(driver, str) and isinstance(rationale, str):
                clusters.append(SharedDriverCluster(tickers, driver, rationale))
        contradictions: list[ThesisContradiction] = []
        for raw in cast("list[object]", p.get("contradictions") or []):
            if not isinstance(raw, dict):
                continue
            r = cast("dict[str, object]", raw)
            tickers = tuple(
                t for t in cast("list[object]", r.get("tickers") or []) if isinstance(t, str)
            )
            contradiction, rationale = r.get("contradiction"), r.get("rationale")
            if tickers and isinstance(contradiction, str) and isinstance(rationale, str):
                contradictions.append(ThesisContradiction(tickers, contradiction, rationale))
        return CollisionReport(
            clusters=tuple(clusters),
            contradictions=tuple(contradictions),
            tickers_analyzed=analyzed,
        )


_PROMPT = """You are auditing a personal equity portfolio for two specific risks that \
position-level statistics (correlation, beta) can miss: names that LOOK independent but \
really share the same underlying causal driver, and theses that make DIRECTLY \
CONTRADICTORY bets.

Below is every holding's thesis and its tier-1 break-rule drivers (the conditions that \
would falsify the thesis).

{ticker_blocks}

Find two things:

1. SHARED-DRIVER CLUSTERS: groups of 2+ tickers whose theses would break together \
because they depend on the SAME underlying driver (a specific macro exposure, a shared \
customer/supplier, a shared regulatory outcome, the same executional bet) — not just the \
same sector or vague "growth stock" similarity. Only report a cluster when you can name \
the SPECIFIC shared driver.

2. CONTRADICTIONS: pairs of tickers whose theses make opposite bets on the same \
underlying question, such that one thesis playing out makes the other LESS likely to be \
right (not just "different industries" — a genuine tension in view).

Be conservative: an empty list for either is a valid and expected answer when the book is \
genuinely diversified. Do not invent a driver or contradiction to have something to report.

Return ONLY this JSON object, nothing else:
{{"clusters": [{{"tickers": ["<TICKER>", "<TICKER>"], "driver": "<specific shared driver, \
one sentence>", "rationale": "<why these specific names, 1-2 sentences>"}}], \
"contradictions": [{{"tickers": ["<TICKER>", "<TICKER>"], "contradiction": "<the opposing \
bet, one sentence>", "rationale": "<why they conflict, 1-2 sentences>"}}]}}"""


def _ticker_block(s: ThesisSnapshot) -> str:
    breaks = (
        "\n".join(f"  - {name}: breaks if {cond}" for name, cond in s.tier1_breaks)
        if s.tier1_breaks
        else "  - (no tier-1 breaks on file)"
    )
    driver_line = f"Key driver: {s.key_driver}\n" if s.key_driver else ""
    return f"### {s.ticker}\n{driver_line}Thesis: {s.thesis}\nTier-1 breaks:\n{breaks}"


def build_prompt(snapshots: Sequence[ThesisSnapshot]) -> str:
    """The whole-book thesis-collision prompt over the given snapshots."""
    blocks = "\n\n".join(_ticker_block(s) for s in snapshots)
    return _PROMPT.format(ticker_blocks=blocks)


# The injectable LLM seam: prompt -> parsed JSON object.
CollisionCall = Callable[[str], dict[str, object]]


def _default_call() -> CollisionCall:
    def call(prompt: str) -> dict[str, object]:
        from llm.structured import call_llm_structured

        payload = call_llm_structured(
            prompt,
            purpose=PURPOSE,
            scope=_SCOPE,
            expect="object",
            required_keys=("clusters", "contradictions"),
        )
        return cast("dict[str, object]", payload)

    return call


def _coerce_tickers(raw: object, known: frozenset[str]) -> tuple[str, ...] | None:
    """A finding's ticker list, validated against the actual analyzed set —
    unknown/hallucinated tickers are dropped; a finding left with fewer than
    2 real tickers is discarded entirely (not a collision)."""
    if not isinstance(raw, list):
        return None
    seen: list[str] = []
    for v in cast("list[object]", raw):
        if isinstance(v, str) and v.strip().upper() in known and v.strip().upper() not in seen:
            seen.append(v.strip().upper())
    return tuple(seen) if len(seen) >= 2 else None


def _coerce_text(raw: object, *, cap: int) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()[:cap]


def propose_thesis_collisions(
    snapshots: Sequence[ThesisSnapshot],
    *,
    call: CollisionCall | None = None,
) -> CollisionReport:
    """Propose the collision findings for ``snapshots``. RAISES
    ``StructuredParseError`` on unusable JSON (the operational wrapper
    degrades it) and lets hard stops propagate. Every finding is validated
    in code against the real ticker set — a hallucinated ticker or a
    single-name "cluster" never reaches the returned report."""
    the_call = call if call is not None else _default_call()
    payload = the_call(build_prompt(snapshots))
    known = frozenset(s.ticker for s in snapshots)

    clusters: list[SharedDriverCluster] = []
    for raw in cast("list[object]", payload.get("clusters") or []):
        if not isinstance(raw, dict):
            continue
        r = cast("dict[str, object]", raw)
        tickers = _coerce_tickers(r.get("tickers"), known)
        driver = _coerce_text(r.get("driver"), cap=200)
        rationale = _coerce_text(r.get("rationale"), cap=_RATIONALE_CAP)
        if tickers and driver and rationale:
            clusters.append(SharedDriverCluster(tickers, driver, rationale))

    contradictions: list[ThesisContradiction] = []
    for raw in cast("list[object]", payload.get("contradictions") or []):
        if not isinstance(raw, dict):
            continue
        r = cast("dict[str, object]", raw)
        tickers = _coerce_tickers(r.get("tickers"), known)
        contradiction = _coerce_text(r.get("contradiction"), cap=200)
        rationale = _coerce_text(r.get("rationale"), cap=_RATIONALE_CAP)
        if tickers and contradiction and rationale:
            contradictions.append(ThesisContradiction(tickers, contradiction, rationale))

    return CollisionReport(
        clusters=tuple(clusters),
        contradictions=tuple(contradictions),
        tickers_analyzed=tuple(sorted(known)),
    )


def generate_thesis_collisions(
    snapshots: Sequence[ThesisSnapshot],
    *,
    call: CollisionCall | None = None,
) -> CollisionReport:
    """Operational entry point: :func:`propose_thesis_collisions`, but
    degrading a ``StructuredParseError`` to an empty report (a transient bad
    response never blocks the pipeline; a budget/setup hard stop still
    propagates). Fewer than 2 snapshots returns empty WITHOUT spending a call
    — there is nothing to collide."""
    from llm.structured import StructuredParseError

    if len(snapshots) < 2:
        return CollisionReport(tickers_analyzed=tuple(sorted(s.ticker for s in snapshots)))
    try:
        return propose_thesis_collisions(snapshots, call=call)
    except StructuredParseError:
        log.warning({"event": "thesis_collision_parse_failed_to_empty", "n": len(snapshots)})
        return CollisionReport(tickers_analyzed=tuple(sorted(s.ticker for s in snapshots)))


# ---------------------------------------------------------------------------
# Cache-aware refresh (the sole write path) + the render-side read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefreshResult:
    report: CollisionReport
    cache_hit: bool  # True = thesis set unchanged since the last run, no LLM spend


def refresh_thesis_collision(
    db_path: Path,
    repo_root: Path,
    tickers: Sequence[str],
    *,
    call: CollisionCall | None = None,
) -> RefreshResult | None:
    """Recompute (or reuse) the whole-book collision report, keyed by a
    thesis-set hash. Returns ``None`` when fewer than 2 names have a usable
    thesis (nothing to analyze). Skips the LLM call entirely when the thesis
    set is unchanged since the last cached run."""
    import llm_artifact_store as store

    snapshots = load_thesis_snapshots(repo_root, tickers)
    if len(snapshots) < 2:
        return None
    inputs = cache_inputs(snapshots)
    prompt_version = _prompt_version()
    fresh_sha = store.compute_input_sha256(prompt_version=prompt_version, cache_inputs=inputs)
    existing = store.read_current(ticker=None, purpose=PURPOSE, scope=_SCOPE, db_path=db_path)
    if existing is not None and existing.input_sha256 == fresh_sha and not existing.dirty:
        cached = CollisionReport.from_json(existing.content_json)
        if cached is not None:
            return RefreshResult(report=cached, cache_hit=True)

    report = generate_thesis_collisions(snapshots, call=call)
    store.upsert(
        store.UpsertRequest(
            ticker=None,
            purpose=PURPOSE,
            scope=_SCOPE,
            content_json=report.to_json(),
            prompt_version=prompt_version,
            cache_inputs=inputs,
        ),
        db_path=db_path,
    )
    return RefreshResult(report=report, cache_hit=False)


def _prompt_version() -> str:
    from llm.prompt_versions import prompt_version_for

    return prompt_version_for(PURPOSE)


@dataclass(frozen=True, slots=True)
class CachedReport:
    report: CollisionReport
    generated_at: str


def read_cached_report(db_path: Path) -> CachedReport | None:
    """The render-side read: the last-persisted collision report, or ``None``
    when nothing has been generated yet. Pure DB read — no LLM, no
    ``financial_facts`` — safe on the render path."""
    import sqlite3

    import llm_artifact_store as store

    try:
        artifact = store.read_current(ticker=None, purpose=PURPOSE, scope=_SCOPE, db_path=db_path)
    except sqlite3.OperationalError:
        # A thin/older-schema DB (offline fixtures, pre-migration prod) has no
        # llm_artifacts table — or one missing newer columns. "Safe on the
        # render path" means the panel renders without the collision section,
        # never 500s (the CI-red root cause after #784).
        return None
    if artifact is None:
        return None
    report = CollisionReport.from_json(artifact.content_json)
    if report is None:
        return None
    return CachedReport(report=report, generated_at=artifact.generated_at.isoformat())


__all__ = [
    "DRIVER_CHAR_CAP",
    "MAX_TIER1_PER_TICKER",
    "PURPOSE",
    "THESIS_CHAR_CAP",
    "CachedReport",
    "CollisionCall",
    "CollisionReport",
    "RefreshResult",
    "SharedDriverCluster",
    "ThesisContradiction",
    "ThesisSnapshot",
    "build_prompt",
    "cache_inputs",
    "generate_thesis_collisions",
    "load_thesis_snapshot",
    "load_thesis_snapshots",
    "propose_thesis_collisions",
    "read_cached_report",
    "refresh_thesis_collision",
]

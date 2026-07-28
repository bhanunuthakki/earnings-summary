"""Business-factor taxonomy — C3, the Workstream C keystone (2026-07-19 program
plan).

Ticker-level correlation/beta answers "does this name move with SPY" but not
"what is this name's THESIS actually betting on" — two holdings with low
price correlation can still be the same underlying bet (NU and MELI both
riding the Brazilian consumer; BKNG and UBER both riding travel demand). This
module grounds every holding onto a SMALL, FIXED, human-curated taxonomy of
business drivers (:data:`TAXONOMY`) via one governed LLM call per name, so the
Risk tab can show a book-level vector: how much of the portfolio is really a
bet on "Brazil consumer credit" vs. "digital ad spend" vs. "GLP-1
reimbursement" etc., independent of ticker count.

Modeled directly on ``src/thesis_collision.py`` (see that module's docstring
for the pattern this mirrors): a governed LLM purpose (``LLM_MODELS`` +
``prompt_versions`` only, per
[[reference_operational_llm_purpose_recipe]]), a code-side grounding gate that
drops anything the model invents rather than trusting it, and an
``llm_artifact_store`` cache keyed by a deterministic input hash so a re-run
with unchanged inputs costs zero LLM spend. It diverges from thesis_collision
in two structural ways:

1. **Scope is per-ticker, not whole-book.** Each holding's loadings are
   independent of every other holding's — there is no cross-name reasoning
   step here (that is thesis_collision's job). The artifact cache is
   therefore scoped ``(ticker, purpose="business_factor_taxonomy")`` rather
   than a single portfolio-wide row.
2. **Findings are PERSISTED, not just cached.** thesis_collision's Risk-tab
   section reads the cached ``llm_artifacts`` row directly. This module also
   writes every derivation into :data:`business_factor_exposures`
   (migration 0196) as an append-only history with an ``owner_edited``
   supremacy flag — the owner can hand-correct a loading (e.g. "no, RBRK's
   AI-capex exposure is overstated") and no future auto-refresh may ever
   clobber that row. ``persist_exposures`` enforces this at the SQL layer;
   see its docstring for the exact skip/never-touch rules.

Inputs per name (never live network — everything is either the C2 segment
rollup already on disk or the ``micro_thesis/holdings/<T>.json`` file used by
thesis_collision):

* the C2 revenue mix (``allocation.exposure.latest_revenue_mix``, geography
  AND product dimensions — a name can disclose one, both, or neither);
* the micro_thesis thesis text + key driver (via
  ``thesis_collision.load_thesis_snapshot`` — reused rather than
  re-implemented, since it already has the truncation caps and the
  no-thesis-file degrade this module needs identically);
* the company name (read directly off the same holdings JSON — a lighter
  substrate than the ``company_description`` LLM artifact, which is itself
  network/10-K-fetch-backed and would make this purpose depend on another
  purpose's cache state).

A name with NEITHER a usable segment mix NOR a thesis file on record has
nothing to ground a call on and is skipped entirely (no LLM spend, no row) —
:func:`derive_factor_exposures` returns ``None``. A name with a thesis but no
mix still gets loadings, tagged ``provenance="thesis_derived"`` — the plan's
explicit allowance for segment-less names (ETFs, or holdings whose 10-K/20-F
crosstab extraction hasn't landed yet).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from allocation.exposure import NameMix, latest_revenue_mix
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from thesis_collision import ThesisSnapshot, load_thesis_snapshot

log = logging.getLogger(__name__)

PURPOSE = "business_factor_taxonomy"
_SCOPE = "ticker"

# Bump when TAXONOMY membership or a definition's meaning materially changes —
# it rides the input_sha (see compute_input_sha), so a taxonomy edit forks
# every cached derivation cleanly instead of silently mixing old-taxonomy and
# new-taxonomy loadings in the same book vector.
TAXONOMY_VERSION = "v1"

# The controlled vocabulary. Deliberately SMALL (12-16, not "however many
# segments exist") — the point is a book-level lens coarse enough to spot
# concentration across names that look unrelated at the ticker level, not a
# re-derivation of each name's segment mix. Tuned to the 11 current equity
# holdings' actual theses (BKNG, BN, MELI, META, NOW, NU, NVO, RBRK, UBER,
# VEEV, WIX); a factor with zero holdings loaded onto it is harmless (it
# simply never appears in the book vector), so this can grow ahead of the
# book without churn. factor -> one-line definition shown to the model so
# loadings are grounded in a stated meaning, not the label alone.
TAXONOMY_DEFINITIONS: dict[str, str] = {
    "Brazil consumer credit": (
        "Revenue or earnings tied to Brazilian consumer lending, credit-card, "
        "or deposit spread economics — sensitive to Selic rate, BRL, and "
        "Brazilian household credit cycles."
    ),
    "LatAm consumer/FX": (
        "Revenue denominated in or economically exposed to Latin American "
        "consumer spending and currencies broadly (Brazil, Argentina, "
        "Colombia) beyond credit specifically — e-commerce, payments, travel."
    ),
    "Mexico expansion": (
        "Growth thesis dependent on a Mexico market-entry or scale-up "
        "(banking license, logistics buildout, new-market customer "
        "acquisition) still ramping rather than mature."
    ),
    "US enterprise IT budgets": (
        "Revenue from US/global enterprise software or IT spend — sensitive "
        "to corporate IT budget cycles, seat/consumption pricing, and "
        "enterprise sales cycles."
    ),
    "AI capex/data-volume": (
        "Revenue that scales with enterprise AI adoption, data volume under "
        "management, or AI-agent proliferation — a demand tailwind from "
        "customers generating/processing more data, not the holding itself "
        "being an AI infrastructure vendor."
    ),
    "digital ad spend": (
        "Revenue from digital advertising — sensitive to marketer budget "
        "cycles, ad-platform competition, and macro ad-spend elasticity."
    ),
    "global travel demand": (
        "Revenue tied to consumer travel volume (flights, lodging, "
        "experiences) — sensitive to discretionary travel spend and global "
        "mobility."
    ),
    "GLP-1/obesity reimbursement": (
        "Revenue or earnings exposed to GLP-1/obesity-drug volume, pricing, "
        "or payer reimbursement policy — a single therapeutic-class demand "
        "and pricing driver."
    ),
    "insulin/diabetes pricing": (
        "Revenue exposed to insulin or broader diabetes-care pricing and "
        "reimbursement dynamics, distinct from the GLP-1 growth story."
    ),
    "SMB web/e-commerce": (
        "Revenue from small-and-medium-business website, storefront, or "
        "e-commerce tooling — sensitive to SMB formation rates and SMB "
        "discretionary software spend."
    ),
    "rides/delivery marketplace": (
        "Revenue from a two-sided rides or delivery marketplace — driver/"
        "courier supply economics, take-rate, and urban mobility demand."
    ),
    "alternative-asset fundraising": (
        "Fee or carry revenue tied to alternative-asset (real assets, "
        "infrastructure, credit, renewables) fundraising cycles and "
        "assets-under-management growth."
    ),
    "rates/duration": (
        "Earnings sensitive to interest-rate level or duration — financing "
        "costs, spread income, or discount-rate-sensitive asset values."
    ),
    "cybersecurity/data-resilience": (
        "Revenue from cybersecurity, backup, or data-resilience software — "
        "sensitive to ransomware/breach frequency and enterprise security "
        "budget prioritization."
    ),
    "life sciences cloud software": (
        "Revenue from software used by pharma/biotech/life-sciences "
        "customers (clinical, regulatory, quality workflows) — sensitive to "
        "life-sciences R&D and commercial spend, not drug pricing itself."
    ),
}
TAXONOMY: tuple[str, ...] = tuple(TAXONOMY_DEFINITIONS)

MAX_LOADINGS_PER_TICKER = 5
_RATIONALE_CAP = 240
_DIM_TYPES: tuple[str, ...] = ("geography", "product")
_HOLDINGS_DIRNAME = ("micro_thesis", "holdings")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FactorLoading:
    """One (factor, loading, rationale) triple for one ticker."""

    factor: str
    loading: float  # 0..1, clamped
    rationale: str


@dataclass(frozen=True, slots=True)
class BookFactorVector:
    """The book-level read: weight x loading summed per factor, plus each
    factor's top-3 contributing tickers (by their weight x loading
    contribution). Pure read over ``is_latest`` rows — zero LLM, safe on the
    render path."""

    vector: dict[str, float]
    top_contributors: dict[str, tuple[tuple[str, float], ...]]


@dataclass(frozen=True, slots=True)
class TickerRefreshResult:
    ticker: str
    loadings: tuple[FactorLoading, ...]
    provenance: str  # "segment_derived" | "thesis_derived"
    cache_hit: bool  # True = LLM call skipped, cached artifact reused
    rows_written: int  # new/superseding rows persist_exposures actually wrote


# ---------------------------------------------------------------------------
# Local-disk inputs (no network, no live LLM)
# ---------------------------------------------------------------------------


def _company_name(repo_root: Path, ticker: str) -> str | None:
    """The company display name straight off the holdings JSON — a much
    lighter substrate than the ``company_description`` LLM artifact (which is
    itself 10-K/profile-fetch-backed); good enough to label the prompt."""
    path = repo_root.joinpath(*_HOLDINGS_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    name = cast("dict[str, object]", payload).get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _thesis_file_sha(repo_root: Path, ticker: str) -> str | None:
    """Raw-bytes sha256 of the holdings JSON, so ANY edit to the file (thesis
    prose, tier-1 KPIs, anything) forks the input_sha — a coarser but simpler
    signal than re-hashing only the fields the prompt actually reads, and
    correct in the direction that matters (never under-invalidates)."""
    path = repo_root.joinpath(*_HOLDINGS_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


def compute_input_sha(
    ticker: str,
    *,
    geo_mix: NameMix | None,
    product_mix: NameMix | None,
    thesis_sha: str | None,
) -> str:
    """Deterministic idempotency key over (sorted mix shares + thesis file
    sha + taxonomy version). Order-independent on the mix shares themselves
    (sorted by dim name) so disk-iteration order never spuriously forks the
    key; order-DEPENDENT on which inputs are present at all (a mix appearing
    or disappearing must change the hash)."""
    h = hashlib.sha256()
    h.update(f"taxonomy={TAXONOMY_VERSION}\n".encode())
    h.update(f"ticker={ticker.upper()}\n".encode())
    for label, mix in (("geo", geo_mix), ("product", product_mix)):
        if mix is None:
            h.update(f"{label}=none\n".encode())
            continue
        h.update(f"{label}_basis={mix.basis}|{mix.period_end}\n".encode())
        for dim_name, share in sorted(mix.shares.items()):
            h.update(f"{label}:{dim_name}={share:.6f}\n".encode())
    h.update(f"thesis_sha={thesis_sha or 'none'}\n".encode())
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class _Inputs:
    name: str | None
    geo_mix: NameMix | None
    product_mix: NameMix | None
    snapshot: ThesisSnapshot | None
    thesis_sha: str | None
    input_sha: str
    provenance: str


def _gather_inputs(ticker: str, *, db_path: Path | str | None, repo_root: Path) -> _Inputs | None:
    """Everything :func:`derive_factor_exposures` / :func:`refresh_ticker_exposures`
    need, assembled once. ``None`` when the name has neither a usable segment
    mix nor a thesis file — nothing to ground a call on, so no LLM spend and
    no artifact/row is ever written for it."""
    ticker = ticker.upper()
    geo_mix = latest_revenue_mix(ticker, dim_type="geography", db_path=db_path)
    product_mix = latest_revenue_mix(ticker, dim_type="product", db_path=db_path)
    snapshot = load_thesis_snapshot(repo_root, ticker)
    if snapshot is None and geo_mix is None and product_mix is None:
        return None
    thesis_sha = _thesis_file_sha(repo_root, ticker)
    input_sha = compute_input_sha(
        ticker, geo_mix=geo_mix, product_mix=product_mix, thesis_sha=thesis_sha
    )
    provenance = (
        "segment_derived"
        if (geo_mix is not None or product_mix is not None)
        else ("thesis_derived")
    )
    return _Inputs(
        name=_company_name(repo_root, ticker),
        geo_mix=geo_mix,
        product_mix=product_mix,
        snapshot=snapshot,
        thesis_sha=thesis_sha,
        input_sha=input_sha,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT = """You are mapping ONE equity holding onto a small, FIXED taxonomy of \
business-driver factors, so a personal portfolio's factor exposure can be read \
across names independent of ticker or sector labels.

Holding: {ticker}

{geo_block}{product_block}{thesis_block}
Taxonomy (choose ONLY from this list — copy the label EXACTLY, do not invent, \
rename, or combine factors):
{taxonomy_block}

For this ONE holding, return up to {max_n} factors it is genuinely exposed to, \
each with a loading from 0 to 1 (how much of this holding's investment case \
that factor explains — they need not sum to 1) and a one-sentence rationale \
grounded in the mix/thesis text above. Do not force a factor that doesn't \
apply just to fill the list; a holding can have as few as 1.

Return ONLY this JSON array, nothing else:
[{{"factor": "<EXACT taxonomy label>", "loading": <0..1>, "rationale": "<one \
sentence, grounded in the text above>"}}]"""


def _mix_block(mix: NameMix | None, label: str) -> str:
    if mix is None:
        return ""
    lines = "\n".join(
        f"  - {name}: {share * 100:.0f}%"
        for name, share in sorted(mix.shares.items(), key=lambda kv: -kv[1])
    )
    return f"{label} revenue mix ({mix.basis}, as of {mix.period_end}):\n{lines}\n"


def _taxonomy_block() -> str:
    return "\n".join(
        f"- {factor}: {definition}" for factor, definition in TAXONOMY_DEFINITIONS.items()
    )


def build_prompt(
    ticker: str,
    *,
    name: str | None,
    geo_mix: NameMix | None,
    product_mix: NameMix | None,
    snapshot: ThesisSnapshot | None,
) -> str:
    label = f"{ticker.upper()} ({name})" if name else ticker.upper()
    geo_block = _mix_block(geo_mix, "Geography") or "(no disclosed geography mix on file)\n"
    product_block = _mix_block(product_mix, "Product") or "(no disclosed product mix on file)\n"
    if snapshot is not None:
        driver_line = f"Key driver: {snapshot.key_driver}\n" if snapshot.key_driver else ""
        thesis_block = f"Thesis: {snapshot.thesis}\n{driver_line}"
    else:
        thesis_block = "Thesis: (no thesis on file for this name — rely on the revenue mix above)\n"
    return _PROMPT.format(
        ticker=label,
        geo_block=geo_block,
        product_block=product_block,
        thesis_block=thesis_block,
        taxonomy_block=_taxonomy_block(),
        max_n=MAX_LOADINGS_PER_TICKER,
    )


# The injectable LLM seam: prompt -> parsed JSON array.
FactorCall = Callable[[str], list[object]]


def _default_call() -> FactorCall:
    def call(prompt: str) -> list[object]:
        from llm.structured import call_llm_structured

        payload = call_llm_structured(prompt, purpose=PURPOSE, expect="array")
        return cast("list[object]", payload)

    return call


def _validate_loadings(raw: object) -> tuple[FactorLoading, ...]:
    """The grounding gate: a factor label not EXACTLY in :data:`TAXONOMY` is
    dropped (and logged — a model that invents factor labels is a
    hallucination-pattern signal worth watching, matching
    ``thesis_collision._coerce_tickers``'s discipline). Loadings are clamped
    to [0, 1]; a factor repeated in the response keeps its higher loading
    (dedup, not append). Output is capped at :data:`MAX_LOADINGS_PER_TICKER`,
    highest loading first."""
    if not isinstance(raw, list):
        return ()
    known = frozenset(TAXONOMY)
    seen: dict[str, FactorLoading] = {}
    dropped: list[str] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        factor_raw = e.get("factor")
        if not isinstance(factor_raw, str) or not factor_raw.strip():
            continue
        factor = factor_raw.strip()
        if factor not in known:
            dropped.append(factor)
            continue
        loading_raw = e.get("loading")
        if not isinstance(loading_raw, (int, float)) or isinstance(loading_raw, bool):
            continue
        loading = max(0.0, min(1.0, float(loading_raw)))
        rationale_raw = e.get("rationale")
        rationale = rationale_raw.strip()[:_RATIONALE_CAP] if isinstance(rationale_raw, str) else ""
        if not rationale:
            continue
        existing = seen.get(factor)
        if existing is None or loading > existing.loading:
            seen[factor] = FactorLoading(factor=factor, loading=loading, rationale=rationale)
    if dropped:
        log.warning(
            {
                "event": "business_factor_dropped_out_of_taxonomy",
                "dropped": dropped,
                "taxonomy_size": len(known),
            }
        )
    ordered = sorted(seen.values(), key=lambda fl: fl.loading, reverse=True)
    return tuple(ordered[:MAX_LOADINGS_PER_TICKER])


def propose_factor_loadings(
    ticker: str,
    *,
    name: str | None,
    geo_mix: NameMix | None,
    product_mix: NameMix | None,
    snapshot: ThesisSnapshot | None,
    call: FactorCall | None = None,
) -> tuple[FactorLoading, ...]:
    """RAISES ``StructuredParseError`` on unusable JSON (the operational
    wrapper below degrades it) and lets hard stops propagate. Every finding
    is validated in code against :data:`TAXONOMY` before it can be returned."""
    the_call = call if call is not None else _default_call()
    prompt = build_prompt(
        ticker, name=name, geo_mix=geo_mix, product_mix=product_mix, snapshot=snapshot
    )
    raw = the_call(prompt)
    return _validate_loadings(raw)


def generate_factor_loadings(
    ticker: str,
    *,
    name: str | None,
    geo_mix: NameMix | None,
    product_mix: NameMix | None,
    snapshot: ThesisSnapshot | None,
    call: FactorCall | None = None,
) -> tuple[FactorLoading, ...]:
    """Operational entry point: :func:`propose_factor_loadings`, but
    degrading a ``StructuredParseError`` to an empty tuple (a transient bad
    response never blocks the refresh sweep for other names; a budget/setup
    hard stop still propagates)."""
    from llm.structured import StructuredParseError

    try:
        return propose_factor_loadings(
            ticker,
            name=name,
            geo_mix=geo_mix,
            product_mix=product_mix,
            snapshot=snapshot,
            call=call,
        )
    except StructuredParseError:
        log.warning({"event": "business_factor_parse_failed_to_empty", "ticker": ticker})
        return ()


def derive_factor_exposures(
    ticker: str,
    *,
    db_path: Path | str | None,
    repo_root: Path,
    call: FactorCall | None = None,
) -> list[FactorLoading] | None:
    """Public single-shot derivation (no artifact cache, no persistence) —
    gathers inputs and makes ONE call. ``None`` when the name has nothing to
    ground a call on (see :func:`_gather_inputs`); an empty list is a valid
    result (parse failure degraded, or the model genuinely found nothing).
    :func:`refresh_ticker_exposures` is the cached/persisted production path;
    this is the pure building block underneath it, exposed directly for
    tests and one-off inspection."""
    inputs = _gather_inputs(ticker, db_path=db_path, repo_root=repo_root)
    if inputs is None:
        return None
    loadings = generate_factor_loadings(
        ticker,
        name=inputs.name,
        geo_mix=inputs.geo_mix,
        product_mix=inputs.product_mix,
        snapshot=inputs.snapshot,
        call=call,
    )
    return list(loadings)


# ---------------------------------------------------------------------------
# Persistence — owner-edit supremacy
# ---------------------------------------------------------------------------


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    """Best-effort connection open, matching ``llm_artifact_store._open`` —
    the LLM call that produced the loadings must never fail because the
    store can't write. busy_timeout=30000 per this repo's operational
    guidance for new portfolio.db connections (a 5s timeout was too tight
    under the daily write load a prior classifier outage traced to)."""
    try:
        from db_paths import resolve_db_path

        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        conn.execute("PRAGMA busy_timeout = 30000")
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='business_factor_exposures'"
        )
        if cur.fetchone() is None:
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError) as exc:
        log.debug({"event": "risk_factors_store_open_failed", "error": str(exc)})
        return None


def persist_exposures(
    ticker: str,
    loadings: Sequence[FactorLoading],
    *,
    provenance: str,
    input_sha: str,
    db_path: Path | str | None,
) -> int:
    """Idempotent, owner-supremacy-respecting persist. Returns the count of
    new rows actually written (0 on a no-op — DB unavailable, nothing to
    write, or the fresh input_sha already matches every existing auto-derived
    row).

    Rules, in order:

    1. **Idempotency**: if every EXISTING auto-derived (``owner_edited=0``)
       latest row for this ticker already carries ``input_sha`` AND the
       factor set is unchanged, skip entirely — a cache-hit LLM re-run must
       never touch this table, not even a superseding no-op write.
    2. **Owner supremacy**: a factor with an existing ``owner_edited=1``
       latest row is SKIPPED — the fresh auto-derived loading for that factor
       is silently dropped (logged at INFO), never written, never
       superseding the owner's row. This is the one rule nothing here may
       violate.
    3. Every other factor in ``loadings`` supersedes its prior latest row (if
       any) via the standard flip-is_latest + set-superseded_by + insert
       pattern, or inserts fresh if there was no prior row.

    A factor that WAS latest before this call but is absent from the new
    ``loadings`` (e.g. it fell out of the top-``MAX_LOADINGS_PER_TICKER``) is
    deliberately left untouched rather than retired — the model returning
    fewer loadings this round is not positive evidence the factor no longer
    applies, only that it wasn't re-asserted; only a fresh call that
    contradicts it, or an owner edit, moves that row.
    """
    if not loadings:
        return 0
    ticker = ticker.upper()
    conn = _open(db_path)
    if conn is None:
        return 0
    try:
        rows = conn.execute(
            "SELECT id, factor, owner_edited, input_sha FROM business_factor_exposures "
            "WHERE ticker = ? AND is_latest = 1",
            (ticker,),
        ).fetchall()
        existing_by_factor = {
            str(r[1]): {"id": int(r[0]), "owner_edited": bool(r[2]), "input_sha": r[3]}
            for r in rows
        }
        auto_existing = {f: v for f, v in existing_by_factor.items() if not v["owner_edited"]}
        new_factor_set = {fl.factor for fl in loadings}
        if (
            auto_existing
            and new_factor_set == set(auto_existing)
            and all(v["input_sha"] == input_sha for v in auto_existing.values())
        ):
            return 0

        now = datetime.now(UTC).isoformat()
        written = 0
        for fl in loadings:
            existing = existing_by_factor.get(fl.factor)
            if existing is not None and existing["owner_edited"]:
                log.info(
                    {
                        "event": "business_factor_owner_edit_protected",
                        "ticker": ticker,
                        "factor": fl.factor,
                    }
                )
                continue
            cur = conn.execute(
                """
                INSERT INTO business_factor_exposures
                    (ticker, factor, loading, rationale, provenance, input_sha,
                     owner_edited, is_latest, created_at, updated_at)
                VALUES (?,?,?,?,?,?,0,1,?,?)
                """,
                (ticker, fl.factor, fl.loading, fl.rationale, provenance, input_sha, now, now),
            )
            new_id = int(cur.lastrowid or 0)
            if existing is not None:
                conn.execute(
                    "UPDATE business_factor_exposures SET is_latest = 0, superseded_by = ?, "
                    "updated_at = ? WHERE id = ?",
                    (new_id, now, existing["id"]),
                )
            written += 1
        conn.commit()
        return written
    except sqlite3.Error as exc:
        log.warning(
            {"event": "business_factor_persist_failed", "ticker": ticker, "error": str(exc)}
        )
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Artifact-cache-aware refresh (mirrors thesis_collision.refresh_thesis_collision)
# ---------------------------------------------------------------------------


def _prompt_version() -> str:
    from llm.prompt_versions import prompt_version_for

    return prompt_version_for(PURPOSE)


def _loadings_to_json(loadings: Sequence[FactorLoading]) -> list[dict[str, object]]:
    return [
        {"factor": fl.factor, "loading": fl.loading, "rationale": fl.rationale} for fl in loadings
    ]


def _loadings_from_json(payload: object) -> tuple[FactorLoading, ...] | None:
    """Parse a previously-cached ``content_json`` blob. ``None`` on any shape
    mismatch (a corrupted/foreign cache row forces a fresh call rather than
    silently serving garbage)."""
    if not isinstance(payload, list):
        return None
    out: list[FactorLoading] = []
    for entry in cast("list[object]", payload):
        if not isinstance(entry, dict):
            return None
        e = cast("dict[str, object]", entry)
        factor, loading_raw, rationale = e.get("factor"), e.get("loading"), e.get("rationale")
        if not isinstance(factor, str) or not isinstance(rationale, str):
            return None
        if not isinstance(loading_raw, (int, float)) or isinstance(loading_raw, bool):
            return None
        out.append(FactorLoading(factor=factor, loading=float(loading_raw), rationale=rationale))
    return tuple(out)


def refresh_ticker_exposures(
    ticker: str,
    *,
    db_path: Path | str | None,
    repo_root: Path,
    call: FactorCall | None = None,
) -> TickerRefreshResult | None:
    """The cached, persisted production path for one name: gather inputs,
    reuse the cached ``llm_artifacts`` row when ``input_sha`` is unchanged
    (zero LLM spend), else make one call and cache the result, then persist
    via :func:`persist_exposures` (which independently no-ops on an unchanged
    input_sha — see its docstring). ``None`` when the name has nothing to
    ground a call on."""
    import llm_artifact_store as store

    ticker = ticker.upper()
    inputs = _gather_inputs(ticker, db_path=db_path, repo_root=repo_root)
    if inputs is None:
        return None

    prompt_version = _prompt_version()
    fresh_artifact_sha = store.compute_input_sha256(
        prompt_version=prompt_version, cache_inputs=[inputs.input_sha]
    )
    existing = store.read_current(ticker=ticker, purpose=PURPOSE, scope=_SCOPE, db_path=db_path)

    loadings: tuple[FactorLoading, ...] | None = None
    cache_hit = False
    if existing is not None and existing.input_sha256 == fresh_artifact_sha and not existing.dirty:
        loadings = _loadings_from_json(existing.content_json)
        cache_hit = loadings is not None

    if loadings is None:
        loadings = generate_factor_loadings(
            ticker,
            name=inputs.name,
            geo_mix=inputs.geo_mix,
            product_mix=inputs.product_mix,
            snapshot=inputs.snapshot,
            call=call,
        )
        store.upsert(
            store.UpsertRequest(
                ticker=ticker,
                purpose=PURPOSE,
                scope=_SCOPE,
                content_json=_loadings_to_json(loadings),
                prompt_version=prompt_version,
                cache_inputs=[inputs.input_sha],
            ),
            db_path=db_path,
        )

    rows_written = persist_exposures(
        ticker, loadings, provenance=inputs.provenance, input_sha=inputs.input_sha, db_path=db_path
    )
    return TickerRefreshResult(
        ticker=ticker,
        loadings=loadings,
        provenance=inputs.provenance,
        cache_hit=cache_hit,
        rows_written=rows_written,
    )


def portfolio_tickers(db_path: Path | str | None) -> list[str]:
    """Every active portfolio-list ticker, alphabetical. ``[]`` when the DB
    is unavailable or has no ``tracked_companies`` table (a thin/offline
    fixture)."""
    from db_paths import resolve_db_path

    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return []
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type = 'portfolio' AND archived_at IS NULL ORDER BY ticker"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [str(r[0]).upper() for r in rows]


def refresh_all(
    db_path: Path | str | None,
    repo_root: Path,
    *,
    call: FactorCall | None = None,
) -> dict[str, int]:
    """Sweep every portfolio holding through :func:`refresh_ticker_exposures`.
    Per-item degrade: a transient failure on one name is tallied and the
    sweep continues (matches the quota-discipline degrade pattern —
    ``execution/run_thesis_collision.py`` siblings); a hard stop (budget
    block, missing CLI) propagates immediately rather than burning through
    the rest of the book against a dead LLM backend."""
    from llm.cli import is_hard_stop

    counts = {
        "tickers": 0,
        "regenerated": 0,
        "cache_hit": 0,
        "skipped_no_input": 0,
        "deferred_transient": 0,
        "rows_written": 0,
    }
    for ticker in portfolio_tickers(db_path):
        counts["tickers"] += 1
        try:
            result = refresh_ticker_exposures(
                ticker, db_path=db_path, repo_root=repo_root, call=call
            )
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.warning(
                {"event": "business_factor_refresh_deferred", "ticker": ticker, "error": str(exc)}
            )
            counts["deferred_transient"] += 1
            continue
        if result is None:
            counts["skipped_no_input"] += 1
            continue
        counts["cache_hit" if result.cache_hit else "regenerated"] += 1
        counts["rows_written"] += result.rows_written
    return counts


# ---------------------------------------------------------------------------
# Book-level read (pure, render-path-safe)
# ---------------------------------------------------------------------------


def book_factor_vector(db_path: Path | str | None, repo_root: Path) -> BookFactorVector:
    """Weights x is_latest loadings, aggregated per factor, with each
    factor's top-3 contributing tickers. Pure DB read + the materialized
    weights cache — zero LLM, zero live tracker call, safe on the render
    path. ``BookFactorVector({}, {})`` when the table/DB is unavailable or no
    weighted holding has any persisted loading yet."""
    from portfolio_weights import read_materialized_weights

    empty = BookFactorVector(vector={}, top_contributors={})
    try:
        weights = read_materialized_weights(repo_root)
    except Exception:
        log.warning({"event": "business_factor_weights_unavailable"}, exc_info=True)
        return empty
    if not weights:
        return empty
    conn = _open(db_path)
    if conn is None:
        return empty
    try:
        rows = conn.execute(
            "SELECT ticker, factor, loading FROM business_factor_exposures WHERE is_latest = 1"
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "business_factor_vector_read_failed", "error": str(exc)})
        return empty
    finally:
        conn.close()

    vector: dict[str, float] = {}
    contributions: dict[str, list[tuple[str, float]]] = {}
    for raw_ticker, raw_factor, raw_loading in rows:
        ticker = str(raw_ticker).upper()
        weight = weights.get(ticker)
        if not weight:
            continue
        try:
            loading = float(raw_loading)
        except (TypeError, ValueError):
            continue
        factor = str(raw_factor)
        contribution = weight * loading
        vector[factor] = vector.get(factor, 0.0) + contribution
        contributions.setdefault(factor, []).append((ticker, contribution))

    top_contributors = {
        factor: tuple(sorted(items, key=lambda kv: kv[1], reverse=True)[:3])
        for factor, items in contributions.items()
    }
    return BookFactorVector(vector=vector, top_contributors=top_contributors)


__all__ = [
    "MAX_LOADINGS_PER_TICKER",
    "PURPOSE",
    "TAXONOMY",
    "TAXONOMY_DEFINITIONS",
    "TAXONOMY_VERSION",
    "BookFactorVector",
    "FactorCall",
    "FactorLoading",
    "TickerRefreshResult",
    "book_factor_vector",
    "build_prompt",
    "compute_input_sha",
    "derive_factor_exposures",
    "generate_factor_loadings",
    "persist_exposures",
    "portfolio_tickers",
    "propose_factor_loadings",
    "refresh_all",
    "refresh_ticker_exposures",
]

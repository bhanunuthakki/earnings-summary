"""execution/import_owner_capacity.py — Tier-A owner-profile import
(tenet-2 Phase 1, docs/design/tenet2_advisory_program.md §3.2 Tier A / §7
decision 1).

Snapshots DERIVED SUMMARIES ONLY from two sibling repos' private planning
files into ``owner_profile_facts``.

Status posture (owner ruling 2026-07-19, refining §7.1): the gated assertion
protects against unratified *inferences about the owner* — but wealthplan and
CIO_CONTEXT are OWNER-AUTHORED sources. The owner typed those values at the
source and vetted them there; re-ratifying them in the Ledger walk is
manufactured work. So facts from ``wealthplan_import`` / ``cio_context_import``
land ``status='affirmed'`` directly, with NO review horizon — freshness comes
from re-running this importer (a changed source value supersedes with a fresh
affirmed row), not from quarterly "still true?" nags. Machine-DERIVED facts
(``--seed-appetite``) remain ``status='proposed'`` and go through the packet
walk: those are inferences, and §7.1 still gates them.

Sources (read-only; this repo's ``data/portfolio.db`` is the only thing ever
written):

  - ``wealthplan/data/plan.local.json``, read via wealthplan's OWN Pydantic
    models (``sys.path`` insertion at this CLI boundary — owner decision
    2026-07-17: "Tier-A import reads wealthplan's models, not hand-copied
    values"). ``wealthplan.persistence.load_plan()`` returns
    ``(Household, Scenario)``; every field this script reads is named
    explicitly below.
  - ``portfolio-tracker/CIO_CONTEXT.local.md``, the human-capital
    correlation bucket-caps table. That file is hand-written prose (no
    structured export exists), so it is parsed by regex against the ONE
    section heading ("## Human-capital correlation buckets") — a rewrite of
    that section is a schema change the parser will need re-pointing at, by
    design (never a silent best-effort guess).

EXCLUDED, never read into a fact (decision 1 — derived summaries only): comp/
bonus/equity-comp amounts (``Person.base_comp/bonus_comp/equity_comp``,
``Promotion.base_bonus_annual/equity_annual``), expense-category line items
(``ExpenseCategory.annual``, rent schedules), mortgage/purchase prices
(``BuyHouseEvent.price`` and its carry terms), exit-payout amounts
(``ExitPayoutEvent.gross_amount/cost_basis``), startup reduced-salary/equity
grants. Every extractor below is a positive allow-list of specific fields —
a source field with no matching extractor here is, by construction, excluded.
Dated life events keep ONLY ``type`` + ``label`` + ``date`` (+ ``end_date``/
``person`` where the event has them); a Promotion with a non-generic label
(e.g. "Quit Meta") becomes a ``career_change`` life event by the SAME rule —
its ``base_bonus_annual``/``equity_annual`` are never touched.

Idempotent: ``owner_profile.store.append_fact`` no-ops when a fact's value is
unchanged from its current latest row — re-running this script against an
unchanged wealthplan/CIO_CONTEXT snapshot writes nothing (logged
``already_done`` per key). Manual/on-demand only; NEVER wire this to a cron —
the source file is the owner's private plan (§3.2 Tier A).

CLI:
    python execution/import_owner_capacity.py --dry-run
    python execution/import_owner_capacity.py
    python execution/import_owner_capacity.py --db-path .tmp/test_profile.db
    python execution/import_owner_capacity.py --seed-appetite --dry-run

``--seed-appetite`` (tenet-2 Phase 2, owner decision 5) additionally stages
ONE ``appetite``-category fact — ``next_dollar.blend_weights`` — seeded
verbatim from ``allocation.model.BLEND_WEIGHTS`` (the current hardcoded
50/30/20 house view), narrated "current hardcoded house view — affirm or
edit" so the owner can ratify or edit it via the existing packet walk. This
reuses ``owner_profile.store.append_fact`` exactly like the capacity facts
above — no new script, no new table.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from owner_profile.models import (  # noqa: E402
    CashBufferMonths,
    EquityFraction,
    GlidePosture,
    HomeCity,
    HorizonAges,
    HumanCapitalBucket,
    LifeEventFact,
    NextDollarBlendWeights,
    ParentCareWindow,
    TaxBucketBalances,
)
from owner_profile.store import append_fact  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_DEFAULT_WEALTHPLAN_ROOT = Path(r"C:\Users\Bhanu\.gemini\antigravity\scratch\wealthplan")
_DEFAULT_CIO_CONTEXT_PATH = Path(
    r"C:\Users\Bhanu\.gemini\antigravity\scratch\portfolio-tracker\CIO_CONTEXT.local.md"
)


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


@dataclass(frozen=True, slots=True)
class StagedFact:
    """One fact ready to append — the importer's own boundary object, decoupled
    from the store's ``append_fact`` kwargs so ``--dry-run`` can print the
    exact same list that a real run would write."""

    category: str
    key: str
    value: dict[str, object]
    narrative: str
    provenance: str
    source_detail: str | None
    # Owner-authored sources (wealthplan, CIO_CONTEXT) land affirmed with no
    # review horizon; only machine-derived facts default to the proposed gate.
    status: str = "affirmed"
    review_horizon_days: int | None = None


# ---------------------------------------------------------------------------
# wealthplan/data/plan.local.json  ->  Tier-A capacity facts
# ---------------------------------------------------------------------------


def _slug_date(d: date) -> str:
    return d.isoformat().replace("-", "_")


def _life_event_key(kind: str, event_date: date, person: str | None) -> str:
    parts = [kind]
    if person:
        parts.append(person)
    parts.append(_slug_date(event_date))
    return "life_event." + "_".join(parts)


def stage_wealthplan_facts(
    wealthplan_root: Path = _DEFAULT_WEALTHPLAN_ROOT,
) -> list[StagedFact]:
    """Read ``wealthplan/data/plan.local.json`` through wealthplan's own
    Pydantic models and stage the Tier-A capacity facts §3.2 names: tax-bucket
    totals, equity fraction, cash-buffer months, glide posture, horizon ages,
    dated life events, home city. Returns ``[]`` (never raises) when the plan
    file doesn't exist — a fresh machine or a moved wealthplan checkout is a
    quiet no-op, not a crash."""
    src = str(wealthplan_root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from wealthplan.models import (
            BabyEvent,
            BuyHouseEvent,
            ExitPayoutEvent,
            MoveCityEvent,
            ParentCareEvent,
            StartupEvent,
            WorkBreakEvent,
        )
        from wealthplan.persistence import load_plan
    except ImportError as exc:
        _log("wealthplan_import_unavailable", reason=str(exc))
        return []

    plan = load_plan()
    if plan is None:
        _log("wealthplan_plan_missing", root=str(wealthplan_root))
        return []
    household, baseline = plan
    source = f"wealthplan/data/plan.local.json as_of={household.starting.as_of.isoformat()}"
    facts: list[StagedFact] = []

    # -- tax-bucket totals + as_of --------------------------------------
    balances = {bucket.value: amount for bucket, amount in household.starting.balances.items()}
    tb = TaxBucketBalances(balances=balances, as_of=household.starting.as_of)
    narrative = "Tax-bucket balances as of {}: {}".format(
        tb.as_of.isoformat(),
        ", ".join(f"{k}=${v:,.0f}" for k, v in sorted(tb.balances.items())),
    )
    facts.append(
        StagedFact(
            category="capacity",
            key="tax_bucket_balances",
            value=tb.model_dump(mode="json"),
            narrative=narrative,
            provenance="wealthplan_import",
            source_detail=source,
        )
    )

    # -- equity fraction --------------------------------------------------
    ef = EquityFraction(equity_fraction=household.starting.equity_fraction)
    facts.append(
        StagedFact(
            category="capacity",
            key="equity_fraction",
            value=ef.model_dump(mode="json"),
            narrative=f"{ef.equity_fraction:.0%} of the investable book is in equity.",
            provenance="wealthplan_import",
            source_detail=source,
        )
    )

    # -- cash-buffer months ------------------------------------------------
    cb = CashBufferMonths(months=household.retirement.cash_buffer_months)
    facts.append(
        StagedFact(
            category="capacity",
            key="cash_buffer_months",
            value=cb.model_dump(mode="json"),
            narrative=f"Target cash buffer: {cb.months:g} months of spend.",
            provenance="wealthplan_import",
            source_detail=source,
        )
    )

    # -- glide posture ------------------------------------------------------
    gp = GlidePosture(
        equity_accumulation=household.glide.equity_accumulation,
        equity_retirement=household.glide.equity_retirement,
        derisk_years=household.glide.derisk_years,
    )
    facts.append(
        StagedFact(
            category="capacity",
            key="glide_posture",
            value=gp.model_dump(mode="json"),
            narrative=(
                f"Glide path: {gp.equity_accumulation:.0%} equity while accumulating -> "
                f"{gp.equity_retirement:.0%} at retirement, de-risking over "
                f"{gp.derisk_years:g} years."
            ),
            provenance="wealthplan_import",
            source_detail=source,
        )
    )

    # -- horizon ages ---------------------------------------------------
    ha = HorizonAges(
        target_retirement_age=household.retirement.target_retirement_age,
        horizon_age=household.retirement.horizon_age,
    )
    facts.append(
        StagedFact(
            category="capacity",
            key="horizon_ages",
            value=ha.model_dump(mode="json"),
            narrative=(
                f"Target retirement age {ha.target_retirement_age}; "
                f"plan horizon age {ha.horizon_age}."
            ),
            provenance="wealthplan_import",
            source_detail=source,
        )
    )

    # -- home city ------------------------------------------------------
    hc = HomeCity(city=household.home_city)
    facts.append(
        StagedFact(
            category="capacity",
            key="home_city",
            value=hc.model_dump(mode="json"),
            narrative=f"Home city: {hc.city}.",
            provenance="wealthplan_import",
            source_detail=source,
        )
    )

    # -- dated life events (type + label + date ONLY — no amounts) ------
    for event in baseline.events:
        life: LifeEventFact | None = None
        if isinstance(event, BabyEvent):
            life = LifeEventFact(kind="baby", label=event.label, date=event.birth_date)
        elif isinstance(event, BuyHouseEvent):
            life = LifeEventFact(kind="buy_house", label=event.label, date=event.purchase_date)
        elif isinstance(event, MoveCityEvent):
            life = LifeEventFact(
                kind="move", label=f"{event.label} to {event.to_city}", date=event.move_date
            )
        elif isinstance(event, WorkBreakEvent):
            life = LifeEventFact(
                kind="work_break",
                label=event.label,
                date=event.start_date,
                end_date=event.end_date,
                person=event.person.value,
            )
        elif isinstance(event, StartupEvent):
            life = LifeEventFact(
                kind="startup",
                label=event.label,
                date=event.start_date,
                end_date=event.end_date,
                person=event.person.value,
            )
        elif isinstance(event, ExitPayoutEvent):
            life = LifeEventFact(kind="exit_payout", label=event.label, date=event.payout_date)
        elif isinstance(event, ParentCareEvent):
            pc = ParentCareWindow(
                label=event.label, start_age=event.start_age, end_age=event.end_age
            )
            facts.append(
                StagedFact(
                    category="capacity",
                    key=f"life_event.parent_care_{pc.start_age}_{pc.end_age}",
                    value=pc.model_dump(mode="json"),
                    narrative=(f"{pc.label}: active from age {pc.start_age} to age {pc.end_age}."),
                    provenance="wealthplan_import",
                    source_detail=source,
                )
            )
            continue
        if life is None:
            continue  # an event type this importer doesn't yet recognize
        window = f" through {life.end_date.isoformat()}" if life.end_date else ""
        who = f" ({life.person})" if life.person else ""
        facts.append(
            StagedFact(
                category="capacity",
                key=_life_event_key(life.kind, life.date, life.person),
                value=life.model_dump(mode="json"),
                narrative=f"{life.label}{who}: {life.date.isoformat()}{window}.",
                provenance="wealthplan_import",
                source_detail=source,
            )
        )

    # -- career-change life events, from non-generic Promotion labels ----
    # A Promotion whose label is empty/"Promotion" is just a routine comp
    # step (excluded); a distinctive label ("Quit Meta") is a career life
    # event worth capturing — its comp fields are never read here.
    for person_key, person in (
        ("person_a", household.person_a),
        ("person_b", household.person_b),
    ):
        for promo in person.promotions:
            label = (promo.label or "").strip()
            if not label or label == "Promotion":
                continue
            facts.append(
                StagedFact(
                    category="capacity",
                    key=_life_event_key("career_change", promo.effective, person_key),
                    value=LifeEventFact(
                        kind="career_change",
                        label=f"{person.name}: {label}",
                        date=promo.effective,
                        person=person_key,
                    ).model_dump(mode="json"),
                    narrative=f"{person.name}: {label} ({promo.effective.isoformat()}).",
                    provenance="wealthplan_import",
                    source_detail=source,
                )
            )

    return facts


# ---------------------------------------------------------------------------
# portfolio-tracker/CIO_CONTEXT.local.md  ->  human-capital bucket caps
# ---------------------------------------------------------------------------

_BUCKET_HEADING = "## Human-capital correlation buckets"
_NEXT_HEADING_RE = re.compile(r"\n(?:---|## )")
_BUCKET_RE = re.compile(
    r"-\s+\*\*`([a-z_]+)`\*\*\s+\(cap\s+\*\*(\d+(?:\.\d+)?)%\*\*\)(.*?)(?=\n-\s+\*\*`|\Z)",
    re.DOTALL,
)
_INCLUDES_RE = re.compile(r"Includes\s+(.*?)\.", re.DOTALL)
_REVIEWED_RE = re.compile(r"_Last reviewed:\s*(\d{4}-\d{2}-\d{2})_")
_TICKER_RE = re.compile(r"^[A-Z]{1,6}$")


def _parse_bucket_members(body: str) -> list[str]:
    m = _INCLUDES_RE.search(body)
    if not m:
        return []
    raw = " ".join(m.group(1).split())  # collapse the source's line-wraps
    members: list[str] = []
    for token in raw.split(","):
        token = re.sub(r"\(.*?\)", "", token).strip()
        for piece in token.split("/"):  # "GOOG/GOOGL" -> two tickers
            piece = piece.strip()
            if _TICKER_RE.match(piece):
                members.append(piece)
    return members


def stage_cio_context_facts(
    cio_context_path: Path = _DEFAULT_CIO_CONTEXT_PATH,
) -> list[StagedFact]:
    """Parse the human-capital correlation bucket-caps table out of the
    tracker's CIO_CONTEXT.local.md prose. Returns ``[]`` (never raises) when
    the file is missing or the one heading this parser keys off has moved —
    a rewritten section is a schema change that needs re-pointing this
    parser, not a silent guess."""
    if not cio_context_path.exists():
        _log("cio_context_missing", path=str(cio_context_path))
        return []
    text = cio_context_path.read_text(encoding="utf-8")
    reviewed = _REVIEWED_RE.search(text)
    source = (
        f"portfolio-tracker/CIO_CONTEXT.local.md last_reviewed={reviewed.group(1)}"
        if reviewed
        else "portfolio-tracker/CIO_CONTEXT.local.md"
    )

    start = text.find(_BUCKET_HEADING)
    if start == -1:
        _log("cio_context_bucket_heading_not_found", path=str(cio_context_path))
        return []
    body_start = start + len(_BUCKET_HEADING)
    end_match = _NEXT_HEADING_RE.search(text, body_start)
    section = text[body_start : end_match.start() if end_match else len(text)]

    facts: list[StagedFact] = []
    for match in _BUCKET_RE.finditer(section):
        bucket_key, cap_str, body = match.group(1), match.group(2), match.group(3)
        members = _parse_bucket_members(body)
        hc = HumanCapitalBucket(cap_pct=float(cap_str), members=members)
        facts.append(
            StagedFact(
                category="capacity",
                key=f"human_capital.{bucket_key}",
                value=hc.model_dump(mode="json"),
                narrative=(
                    f"Human-capital bucket '{bucket_key}': cap {hc.cap_pct:g}% — "
                    f"{', '.join(hc.members) if hc.members else '(no members parsed)'}."
                ),
                provenance="cio_context_import",
                source_detail=source,
            )
        )
    if not facts:
        _log("cio_context_no_buckets_parsed", path=str(cio_context_path))
    return facts


# ---------------------------------------------------------------------------
# --seed-appetite: the hardcoded next-dollar blend, staged for ratification
# ---------------------------------------------------------------------------


def stage_appetite_seed_facts() -> list[StagedFact]:
    """Stage ONE ``proposed`` appetite fact — ``next_dollar.blend_weights`` —
    seeded from the CURRENT hardcoded house view (``allocation.model.
    BLEND_WEIGHTS``), so the owner can ratify or edit it via the existing
    packet walk (owner decision 5, §7 of tenet2_advisory_program.md: "Hardcoded
    50/30/20 becomes the labeled no-profile fallback only").

    Reads ``BLEND_WEIGHTS`` directly (never hand-copies the numbers) so this
    seed can never drift from the model's actual fallback. Idempotent like
    every other staged fact: re-running this flag when ``BLEND_WEIGHTS``
    hasn't changed and the fact hasn't been superseded is a no-op via
    ``owner_profile.store.append_fact``'s value-equality check.
    """
    from allocation.model import BLEND_WEIGHTS

    weights = NextDollarBlendWeights(
        ret=BLEND_WEIGHTS["ret"], div=BLEND_WEIGHTS["div"], macro=BLEND_WEIGHTS["macro"]
    )
    narrative = (
        f"Next-dollar blend weights: ret {weights.ret:.0%} / div {weights.div:.0%} / "
        f"macro {weights.macro:.0%} — current hardcoded house view — affirm or edit."
    )
    return [
        StagedFact(
            category="appetite",
            key="next_dollar.blend_weights",
            value=weights.model_dump(mode="json"),
            narrative=narrative,
            provenance="derived",
            source_detail="allocation.model.BLEND_WEIGHTS",
            # Machine-derived = an inference, so the §7.1 gate still applies.
            status="proposed",
            # Appetite facts review on a POLICY event (pledge >$10k, drawdown
            # >15%), not a fixed day-count cadence — §3.3 of the strategy doc.
            review_horizon_days=None,
        )
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _apply_facts(db_path: Path, facts: list[StagedFact]) -> dict[str, int]:
    """Write every staged fact via the idempotent store; returns a
    {new, unchanged} tally. Runs in ONE transaction so a mid-batch failure
    never leaves a half-imported snapshot."""
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    tally = {"new": 0, "unchanged": 0}
    try:
        for fact in facts:
            before = conn.execute(
                "SELECT id FROM owner_profile_facts WHERE category = ? AND key = ? "
                "AND is_latest = 1",
                (fact.category, fact.key),
            ).fetchone()
            fact_id = append_fact(
                conn,
                category=fact.category,
                key=fact.key,
                value=fact.value,
                narrative=fact.narrative,
                provenance=fact.provenance,
                status=fact.status,
                review_horizon_days=fact.review_horizon_days,
                source_detail=fact.source_detail,
            )
            is_new = before is None or int(cast("int", before["id"])) != fact_id
            tally["new" if is_new else "unchanged"] += 1
            _log(
                "fact_staged" if is_new else "fact_unchanged",
                category=fact.category,
                key=fact.key,
                fact_id=fact_id,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--db-path", type=Path, default=None, help="Defaults to <repo-root>/data/portfolio.db."
    )
    parser.add_argument("--wealthplan-root", type=Path, default=_DEFAULT_WEALTHPLAN_ROOT)
    parser.add_argument("--cio-context-path", type=Path, default=_DEFAULT_CIO_CONTEXT_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be staged; write nothing.",
    )
    parser.add_argument(
        "--seed-appetite",
        action="store_true",
        help=(
            "Also stage the ONE next_dollar.blend_weights appetite fact, seeded from "
            "allocation.model.BLEND_WEIGHTS, for the owner to ratify/edit via the packet walk."
        ),
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )

    wealthplan_facts = stage_wealthplan_facts(args.wealthplan_root)
    cio_facts = stage_cio_context_facts(args.cio_context_path)
    appetite_facts = stage_appetite_seed_facts() if args.seed_appetite else []
    facts = wealthplan_facts + cio_facts + appetite_facts

    _log(
        "staged",
        n_wealthplan=len(wealthplan_facts),
        n_cio_context=len(cio_facts),
        n_appetite=len(appetite_facts),
        keys=[f.key for f in facts],
    )

    if args.dry_run:
        summary = {
            "event": "dry_run",
            "n_staged": len(facts),
            "facts": [
                {
                    "category": f.category,
                    "key": f.key,
                    "provenance": f.provenance,
                    "narrative": f.narrative,
                }
                for f in facts
            ],
        }
        print(json.dumps(summary, indent=2))
        return 0

    if not facts:
        print(json.dumps({"event": "nothing_to_import"}))
        return 0

    if not db_path.exists():
        _log("halt", reason=f"db_path does not exist: {db_path}")
        print(json.dumps({"event": "halt", "reason": f"db_path does not exist: {db_path}"}))
        return 1

    tally = _apply_facts(db_path, facts)
    result = {"event": "import_done", "db_path": str(db_path), **tally}
    _log("import_done", db_path=str(db_path), **tally)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Owner-Context Federation — Authority Map & Read Contracts

**Status:** Phase 1 deliverable (tenet-2 advisory program, `docs/design/tenet2_advisory_program.md` §3.4).
Ships alongside the `owner_profile_facts` migration (0159), the store
(`src/owner_profile/`), and the Tier-A importer
(`execution/import_owner_capacity.py`).

This document is the concrete contract for how earnings-summary (ES) reads
owner-context from its two sibling repos — wealthplan and portfolio-tracker —
expanding the authority map and read contracts sketched in §3.4 of the
strategy doc into something an implementer can point at.

---

## 1. Why federation, not a rebuild

The owner-context layer is substantially **read from** the two sibling
systems, not re-implemented here. A read-only sweep (2026-07-17, cited
verbatim in §3.4) found:

- The ES → wealthplan `book_cma.json` export (`execution/export_book_cma.py`)
  has **zero consumers** in wealthplan's tree — a dead leg, built but never
  wired up on the wealthplan side.
- wealthplan's `tracker.py` reads the portfolio-tracker's SQLite file
  **directly**, bypassing the tracker's own REST API — a schema-drift and
  file-lock risk owned by wealthplan, not ES.
- The ES tracker client works but coerces tracker payloads via a `_f()`
  helper with known pct-vs-fraction and Decimal-string hazards (see
  `reference_tracker_analytics_api.md` in the memory index).
- The `PortfolioTrackerApiServer` logon task exists and is **Ready** — the
  "no persistent process" audit claim from an earlier review is stale as of
  2026-07-16; the residual issue is cold-start latency, already handled by
  the tracker client's tiered-timeout + `probe_tracker` design.

Rebuilding any of this inside ES would duplicate state that already has an
owner (a different repo, a different process) and would re-introduce the
exact sync-drift risk the "one canonical home, no bidirectional sync" design
principle (§3.1.4) exists to avoid.

---

## 2. Authority map

One owner per fact class. No repo writes into another repo's tables.

| System | Authoritative for | Read by |
|---|---|---|
| **portfolio-tracker** | Positions, lots, transactions, accounts + tax treatment, benchmark/return/risk math, realized gains / exit quality. | ES (existing REST client, `src/tracker_client.py` or equivalent); wealthplan (today: direct SQLite read — a wealthplan-side follow-up to migrate onto the tracker's API or a versioned export). |
| **wealthplan** | Household model: comp/bonus/equity ×2 (person_a/person_b), `ContributionPolicy`, `ExpensePlan` + cost-of-living, `RetirementSettings`/glide path, life events (baby/house/move/work-break/startup/parent-care/exit payout), capital-market assumptions (CMAs). | ES **Tier-A import** (derived summaries only — this document's §4); a future ES **capacity reader** for near-term cash-need/expense schedules (Phase 2). |
| **earnings-summary** | Theses, research, decisions/grading, advisory memos, the **owner profile** (`owner_profile_facts` — capacity/appetite/behavioral, affirmed facts only condition advice), Tenets. | The tracker's own CIO advisor stack (its own codebase, per the 2026-06 governance decision — ES does not feed the tracker anything beyond the existing `book_cma.json` export); wealthplan (same export). |

**Corollary:** `owner_profile_facts` is canonical for advisory-relevant
household/appetite/behavioral context (owner decision 2, §7 of the strategy
doc). `CIO_CONTEXT.local.md` remains the tracker's own coaching input — this
program imports *from* it one-way; nothing in this program writes back to it
or to the tracker's database.

---

## 3. Read contracts

Every leg is (a) typed at the boundary, (b) explicit about units, and (c)
degrades to "unavailable" rather than raising or guessing.

### 3.1 ES ← wealthplan (`execution/import_owner_capacity.py`, Phase 1 — SHIPPED)

| Field | Wealthplan source | ES fact key | Type / units |
|---|---|---|---|
| Tax-bucket balances | `Household.starting.balances` (`dict[TaxBucket, float]`) + `.as_of` | `capacity.tax_bucket_balances` | `TaxBucketBalances{balances: dict[str,float] (USD, absolute), as_of: date}` |
| Equity fraction | `Household.starting.equity_fraction` | `capacity.equity_fraction` | `EquityFraction{equity_fraction: float [0,1]}` (fraction, not pct) |
| Cash buffer | `Household.retirement.cash_buffer_months` | `capacity.cash_buffer_months` | `CashBufferMonths{months: float}` |
| Glide posture | `Household.glide.{equity_accumulation,equity_retirement,derisk_years}` | `capacity.glide_posture` | `GlidePosture{equity_accumulation: float [0,1], equity_retirement: float [0,1], derisk_years: float}` |
| Horizon ages | `Household.retirement.{target_retirement_age,horizon_age}` | `capacity.horizon_ages` | `HorizonAges{target_retirement_age: int, horizon_age: int}` (years, integer ages) |
| Home city | `Household.home_city` | `capacity.home_city` | `HomeCity{city: str}` |
| Dated life events | `Scenario.events` (the baseline scenario's `LifeEvent` union: `BabyEvent`, `BuyHouseEvent`, `MoveCityEvent`, `WorkBreakEvent`, `StartupEvent`, `ExitPayoutEvent`) | `capacity.life_event.<kind>[_<person>]_<yyyy_mm_dd>` | `LifeEventFact{kind, label, date, end_date?, person?}` — **type + label + date ONLY**, no dollar fields |
| Parent-care window | `ParentCareEvent.{label,start_age,end_age}` | `capacity.life_event.parent_care_<start>_<end>` | `ParentCareWindow{label, start_age: int, end_age: int}` — age-keyed, not date-keyed |
| Career-change events | Non-generic `Person.promotions[].label` (e.g. "Quit Meta") + `.effective` | `capacity.life_event.career_change_<person>_<yyyy_mm_dd>` | `LifeEventFact{kind="career_change", label, date, person}` |

**Excluded, by construction** (owner decision 1 — derived summaries only): every
comp/bonus/equity-comp field (`Person.base_comp/bonus_comp/equity_comp`,
`Promotion.base_bonus_annual/equity_annual`), every expense-category line item
(`ExpenseCategory.annual`, rent schedules), mortgage/purchase prices
(`BuyHouseEvent.price` and its carry terms), exit-payout amounts
(`ExitPayoutEvent.gross_amount/cost_basis`), and startup reduced-salary/equity
grants. The importer is a positive allow-list — a wealthplan field with no
row in the table above is, by construction, never read into a fact.

**Read mechanics:** `wealthplan.persistence.load_plan()` via a `sys.path`
insertion at the CLI boundary into wealthplan's *own* Pydantic models (owner
decision 2026-07-17: read the models, not hand-copied values). Manual/
on-demand only (`--dry-run` prints staged facts without writing; a real run
appends via `owner_profile.store.append_fact`, idempotent on unchanged
values). Never a cron — the source file is the owner's private plan.

**Degradation:** a missing `plan.local.json`, a wealthplan checkout that has
moved, or an import failure inside `wealthplan.models`/`wealthplan.persistence`
all degrade to "stage nothing, log the reason" (`stage_wealthplan_facts`
returns `[]`) — never a crash, never a guessed value.

### 3.2 ES ← portfolio-tracker (`CIO_CONTEXT.local.md`, Phase 1 — SHIPPED)

| Field | Tracker source | ES fact key | Type / units |
|---|---|---|---|
| Human-capital bucket caps | The "## Human-capital correlation buckets" prose section: `` - **`<bucket>`** (cap **N%**) — ... Includes A, B, C. `` | `capacity.human_capital.<bucket>` | `HumanCapitalBucket{cap_pct: float (0-100, a percentage, NOT a fraction), members: list[str] (uppercased tickers)}` |

**Read mechanics:** regex against the ONE section heading this parser keys
off (`_BUCKET_HEADING`); a bullet's cap is parsed as a percentage literal
(`cap **15%**` → `cap_pct=15.0`, not `0.15`) — callers must not silently
assume a fraction. Member tickers are extracted from the "Includes ..."
clause, parenthetical annotations stripped, slash-combined tickers
(`GOOG/GOOGL`) split into two members.

**Degradation:** a missing file, a missing heading, or zero parsed buckets
all log a specific reason and stage nothing — this parser has NO fallback
guess, because a rewritten section is a schema change that needs
re-pointing the regex, not a best-effort scrape.

**Not yet read from the tracker (Phase 2 repair items, owned by this
program):**

- **Wealthplan capacity reader** — a near-term cash-need/expense schedule ES's
  `/review` capacity block needs but that lives only in wealthplan's
  `ExpensePlan`/`ContributionPolicy` models. Requires a NEW read contract
  (a derived, dollar-free schedule — the same exclusion discipline as §3.1)
  and is explicitly out of scope for Phase 1.
- **Tracker realized-gain / exit-quality payloads** — ES's existing tracker
  REST client already fetches analytics the tracker computes, but `/review`
  never joins them in. This is a contract-formalization task (replace the
  client's `_f()` ad hoc coercion with typed, units-explicit Pydantic models),
  not a new read — tracked as a Phase 2 repair item, not built here.

### 3.3 Degradation semantics (repo-wide convention, restated here)

Every leg in this document follows the SAME proven pattern the tracker
client and the coach's freshness gate already use:

- **Never raises** across a federation boundary. A read failure returns
  `None`/`[]`/an explicit "unavailable" marker — never an exception that
  propagates into a rendering or decision path.
  `available=False` + a reason string is the shape, not a bare `None`, when
  the caller needs to explain *why* (e.g. `probe_tracker`).
- **Degrades to "as of &lt;date&gt;", not a guess.** A stale or absent fact is
  quoted with its age (or its absence) rather than silently treated as
  current — the same freshness discipline `owner_profile_facts.review_horizon_days`
  and `affirmed_at` exist to support (§3.1.5 of the strategy doc).
- **Loopback-only, no auth.** Every leg here runs on localhost, single-owner,
  no network boundary crossed. Any cross-machine ambition is explicitly out
  of scope until auth is designed first.

---

## 4. Sibling-repo follow-ups (tracked, not built here)

These are real gaps the federation sweep found, but they are **owned by the
sibling repos**, not by this program:

1. **Wire wealthplan to consume `book_cma.json`.** The red-team program built
   this export bridge deliberately; the consumer side was never wired up in
   wealthplan. Fixing this is a wealthplan-side task (read the file, use its
   Monte-Carlo tail stats in wealthplan's own FI test).
2. **Migrate wealthplan's tracker access off direct SQLite.** `wealthplan/src/wealthplan/tracker.py`
   reads the portfolio-tracker's database file directly — a schema-drift and
   file-lock risk. The fix (route through the tracker's REST API, or accept a
   versioned export) is a wealthplan-side task; ES's `PortfolioTrackerApiServer`
   is already Ready to serve that API.

---

## 5. What Phase 1 does NOT do

No prompt injection — the `owner_profile` anchor slot in
`compose_anchor_block` is Phase 2. No PreAnalysis capacity block. No
wealthplan capacity reader (near-term cash-need schedule). No tracker
realized-gain/exit-quality join. No behavioral-layer (Tier C) derivation. Only
the substrate (migration + store), the two Tier-A importers, and the
packet-walk ratification surface ship in this PR.

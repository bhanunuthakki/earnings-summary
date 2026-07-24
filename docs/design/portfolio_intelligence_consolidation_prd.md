# Earnings Summary Portfolio Intelligence Consolidation — Product Requirements Document

**Status:** Proposed for owner approval on 2026-07-23.
**Audience:** Repository owner and implementation agents in `earnings-summary`,
`portfolio-tracker`, and `wealthplan`.
**Scope:** Consolidate the owner's user-facing investing system into Earnings Summary:
absorb the analysis, journaling, coaching, and decision surfaces that Portfolio Tracker
currently owns, and consume Portfolio Tracker exclusively through its versioned
`/api/v1` Portfolio Data Service contract.
**Provider companion:**
`portfolio-tracker/docs/design/portfolio_data_service_prd.md`.
**Related consumer companion:**
`wealthplan/docs/design/portfolio_tracker_api_migration_prd.md`.
**Umbrella product PRD:**
`earnings-summary/docs/design/personal_investment_partner_prd.md` (ratified
2026-07-23). This consolidation PRD supplies the cross-repository substrate that the
Personal Investment Partner program builds on; where the two overlap, the Partner PRD
owns product behavior and this PRD owns the repository boundary and migration.
**Document authority:** This PRD does not authorize a live database migration,
backfill, destructive correction, directive edit, or scheduled-task change. Each
state-changing step requires the repository's normal preview, backup, verification,
and approval controls. It is a design artifact, not a Layer-1 directive; it does not
authorize edits to `directives/`.

---

## 1. Executive summary

Earnings Summary becomes the owner's primary user-facing investing system. Portfolio
Tracker becomes a backend Portfolio Data Service that owns observed financial facts and
deterministic derivations behind a versioned HTTP API and retains only a narrow
operations console.

Today the coupling between the two repositories is bidirectional:

- Earnings Summary reads Portfolio Tracker through a REST client
  (`src/integrations/portfolio_tracker_client.py`) — the correct direction, but against
  unversioned endpoints with ad hoc numeric coercion.
- Portfolio Tracker reads Earnings Summary's SQLite database file directly
  (`portfolio-tracker/src/portfolio_tracker/services/earnings_summary.py`, opened
  read-only) to power its Cockpit, thesis-health display, CIO advisor, coaching, and
  holdings enrichment — the wrong direction, and the reason Portfolio Tracker still
  behaves like an analysis product.

This program collapses the coupling to one supported direction:

> Earnings Summary consumes Portfolio Tracker only through `/api/v1`. Portfolio
> Tracker never reads Earnings Summary's database, files, or artifacts.

To get there, Earnings Summary absorbs the user-facing capabilities Portfolio Tracker
currently hosts — the action queue, the trade/decision journal, trade analysis and
timeline, the CIO advisor and monthly briefs, coaching, human-capital interpretation,
and the owner-intent portion of policy/posture — and re-homes each inside the
information architecture the Personal Investment Partner PRD already defines (Today,
Portfolio → Allocation, Portfolio → Health, Review → Ledger, Ask). This is a
capability migration, not a pixel-for-pixel port of Portfolio Tracker's React
frontend.

Wealthplan is unaffected by this document except as a co-consumer of the same
`/api/v1` contract; its migration is specified in its own companion PRD.

---

## 2. Vocabulary

`DEFINITIONS.md` in this repository is authoritative. This PRD reuses the ratified
terms **Owner Decision**, **Senior Partner Brief**, **Incremental Dollar
Recommendation**, **Investment Decision Card**, **Risk Budget**, **Portfolio
Posture**, **Concentration Zone**, **Decision Draft**, **Worldview**, and **Tenet**
verbatim.

It introduces no new domain term of its own. Two provider-side terms appear by
reference:

- **Portfolio Data Service** — Portfolio Tracker's backend-first operating mode,
  defined in the provider PRD and to be added to `portfolio-tracker/DEFINITIONS.md`
  in Phase 0.
- **Legacy provenance artifact** — used descriptively here for imported historical
  records (action-queue history, CIO sessions, monthly briefs) that are preserved
  read-only rather than becoming active Earnings Summary objects. If implementation
  needs it as a typed concept, add it to `DEFINITIONS.md` before coding.

---

## 3. Product intent

### 3.1 Product promise

After this consolidation, every investing question the owner asks — what do I hold,
how is it performing, what is my risk, what should I do next, what did I decide
before, and what should I learn — is answered in Earnings Summary, grounded in:

- deterministic portfolio facts served by the Portfolio Data Service, with explicit
  freshness, coverage, and methodology metadata; and
- research, valuation, thesis, decision, and owner-context facts that Earnings
  Summary itself owns.

Portfolio Tracker's UI answers only operational questions: are my accounts linked,
did the sync run, is the data healthy, is the backup good.

### 3.2 Posture

- Single-user, localhost, pull-only. No trade execution, order staging, or
  brokerage write of any kind.
- Outputs are decision support, not personalized financial or tax advice.
- Deterministic facts come from the provider API; Earnings Summary composes,
  interprets, and recommends but does not independently recalculate provider-owned
  math as competing facts.
- The governed LLM layer (`src/llm/cli.py`) remains the single entry point for all
  judgment; it may explain or challenge calculated results but may not invent
  holdings, prices, transactions, or tax facts.

---

## 4. Current-state findings

Findings from the 2026-07-23 read-only scan of both repositories.

### 4.1 Earnings Summary today

- A Python CLI/batch pipeline (~90 modules under `src/`, 186 Alembic migrations,
  ~140 scripts under `execution/`) that ingests earnings data, evaluates theses,
  runs DCFs, and renders static per-ticker HTML workspace reports to
  `output/research/{TICKER}/`.
- Interactive surface: the Flask comments/chat server (`execution/comments_server.py`,
  localhost:7421) plus HTMX runtime helpers in `src/ui/`. The `design-system/`
  React sandbox is explicitly not shipped.
- Day-to-day heartbeat: the `cron/` fleet of Windows scheduled tasks (daily fetch and
  brief, weekly synthesis, coach pings, decision nudges, calibration scorecard).
- Database: SQLite at `data/portfolio.db` (same filename as Portfolio Tracker's
  database, a different file), Alembic-managed. Key stores: `tracked_companies`,
  `thesis_state`/`thesis_evaluations`/`thesis_ledger_entries`, `dcf_runs`, `alerts`,
  `decisions` + calibration/conditions/nudges, `owner_profile_facts`,
  `analyst_notes`, KPI tables, `llm_calls`, and the `v_thesis_status` view.
- Portfolio facts arrive through `src/integrations/portfolio_tracker_client.py`
  (`fetch_live_portfolio`, `fetch_transaction_history`, `fetch_portfolio_analytics`,
  `fetch_exit_quality`), consumed across `src/advisor/`, `src/allocation/`,
  `src/ask/`, `src/attribution.py`, and `src/calibration_coach.py`. The client
  degrades to `available=False` rather than raising, but coerces payloads with an
  ad hoc `_f()` helper with known percent-versus-fraction and Decimal-string
  hazards — already tracked as a Phase 2 repair item in
  `docs/design/owner_context_federation.md`.

### 4.2 Portfolio Tracker's read of Earnings Summary (to be removed)

`portfolio-tracker/src/portfolio_tracker/services/earnings_summary.py` opens this
repository's `data/portfolio.db` read-only (path from
`config.earnings_summary_db_path`, default `../earnings-summary/data/portfolio.db`)
and serves: `summary_by_ticker`, `thesis_status_by_ticker` (via `v_thesis_status`),
`latest_verdicts`, `latest_valuations`, `pending_alerts`, `thesis_detail`,
`untracked_holdings`, and brief-file helpers that serve this repository's static
report HTML through Portfolio Tracker routes.

Consumers of that bridge inside Portfolio Tracker: `api/routes/earnings_summary.py`,
`api/routes/portfolio.py` (holdings enrichment), `services/cockpit.py`,
`services/cio_advisor.py`, `services/coaching.py`, `services/trade_analysis.py`.
These consumers are exactly the surfaces this PRD migrates; migrating them is what
makes the bridge deletable.

### 4.3 Portfolio Tracker surfaces and state to absorb

| Capability | Portfolio Tracker implementation | State |
| --- | --- | --- |
| Cockpit / action queue | `api/routes/cockpit.py`, `services/cockpit.py`; pages `Cockpit.tsx`, `ThesisHealth.tsx` | `action_queue` |
| Decision / trade journal | `api/routes/decision_support.py`; pages `Review.tsx`, `TradeAnalysis.tsx`, `TradeTimeline.tsx` | `trade_decisions`, `trade_tags` |
| CIO advisor & monthly brief | `api/routes/cio_advisor.py`, `services/cio_advisor.py`; page `Advisor.tsx` | `chat_sessions`, `chat_turns`, `monthly_briefs`, `monthly_brief_jobs` |
| Coaching | `api/routes/coaching.py`, `services/coaching.py` | (derived) |
| Human capital | `api/routes/human_capital.py` | `human_capital_overlap` |
| Policy / posture | `api/routes/policy.py`, `services/policy.py` | `policy_weights` |
| Process/outcome learning | `services/exit_quality.py`, `trade_analysis.py`, `position_alpha.py`; page `Scorecard.tsx` | (derived; calculations stay in the provider) |
| Earnings calendar display | `api/routes/decision_support.py` (`/earnings/upcoming`) | `earnings_calendar` |

Per the provider PRD, the deterministic calculations behind Scorecard-style surfaces
(exit quality, position alpha, performance, risk) remain in Portfolio Tracker and are
consumed by API; only presentation and interpretation move.

### 4.4 Overlap and duplication to resolve

Several migrating capabilities already have an Earnings Summary counterpart. The
consolidation must land on one implementation per capability, not two:

- Portfolio Tracker's Cockpit signal set overlaps the alerts/inbox/feed read models in
  `src/dashboard/` and the tenet-2 governor moment classes.
- Portfolio Tracker's CIO advisor and monthly brief overlap the ratified Senior
  Partner Brief and Ask.
- Portfolio Tracker's coaching tips overlap `coach_pings` and the governor.
- Portfolio Tracker's `trade_decisions` journal overlaps the `decisions` ledger and
  Owner Decision model.
- Portfolio Tracker's human-capital buckets are already imported into
  `owner_profile_facts` by regex-parsing `CIO_CONTEXT.local.md`
  (`owner_context_federation.md` §3.2) — a fragile read this program replaces with
  native ownership.

---

## 5. Goals and non-goals

### 5.1 Goals

#### G1 — One investing home

Every investing analysis, decision, journaling, and coaching workflow the owner uses
lives in Earnings Summary. Portfolio Tracker's retained UI is operational only.

#### G2 — One supported provider boundary

All live portfolio facts enter through a typed client for the documented `/api/v1`
contract. No Earnings Summary code reads Portfolio Tracker's database, tables, or
files; no Portfolio Tracker code reads Earnings Summary's database, tables, or files.

#### G3 — Owner history preserved

The owner's decision history in Portfolio Tracker (`trade_decisions`, `trade_tags`)
is migrated into the `decisions` ledger with provenance, idempotent import, and
rollback. Advisory history (CIO sessions, monthly briefs, action-queue history) is
preserved as read-only legacy provenance where valuable.

#### G4 — One implementation per capability

Each migrated capability is absorbed into the existing Earnings Summary system that
already owns that job (Senior Partner Brief, governor, decisions ledger,
`owner_profile_facts`) rather than ported as a parallel second system.

#### G5 — Honest inputs

Every surface that renders provider facts shows as-of dates, coverage, and
stale/partial warnings from the shared response envelope. A failed or partial
provider read is never rendered as current.

#### G6 — Governed judgment

Every migrated LLM-backed capability routes through `src/llm/cli.py` with a purpose
key, prompt version, structured schema, budget row, cost logging, and an eval plan —
no direct provider SDK use, no ungoverned subprocess calls.

#### G7 — Reversible migration

Additive first; dual-run parity before cutover; Portfolio Tracker's legacy surfaces
and bridge are removed only after Earnings Summary passes the corresponding
acceptance gate and the rollback window expires.

### 5.2 Non-goals

This program does not:

- move linked-account ingestion, provider credentials, reconciliation, or any
  deterministic portfolio calculation into Earnings Summary;
- recalculate Modified-Dietz returns, benchmark counterfactuals, position alpha,
  drawdown, Correlation/beta, Concentration measures, or exit-quality facts locally;
- port Portfolio Tracker's React frontend, add a SPA, or introduce a new top-level
  navigation destination (Partner PRD §3.2 non-goals apply);
- rebuild thesis/KPI tracking, the owner-memory store, or the decision journal as
  new systems;
- change projection, valuation, or tax methodology anywhere;
- give Earnings Summary mutation authority over accounts, syncs, or source
  corrections (operator mutations stay in Portfolio Tracker's console; at most,
  Earnings Summary links to it);
- build multi-user tenancy, public hosting, or brokerage execution; or
- alter Wealthplan (covered by its own companion PRD).

---

## 6. Ownership boundary

### 6.1 Earnings Summary owns after consolidation

- Research, theses, KPIs, DCF and valuation, bear cases, comparable sets.
- The Owner Decision journal, decision calibration, process/outcome learning.
- The action queue / attention surface, Senior Partner Brief, Ask, coaching,
  proactive governance.
- Owner context: `owner_profile_facts`, Portfolio Posture (owner intent), Worldview,
  Tenets, human-capital interpretation.
- All user-facing presentation of portfolio, performance, risk, and benchmark facts.

### 6.2 Portfolio Tracker owns (consumed by API)

- Linked accounts, canonical account identity, holdings, snapshots, transactions,
  cash flows, Securities, prices, benchmarks, corporate actions, corrections.
- Deterministic calculations: performance, benchmark counterfactuals, position-level
  P&L and alpha, drawdown, Correlation/beta, volatility, Positioning and
  Concentration facts, attribution, after-tax primitives, exit-quality facts.
- Sync health, data quality, freshness, coverage, backup/restore.
- Benchmark configuration required for calculation (a calculation setting, not owner
  intent).

### 6.3 Prohibited leakage

Earnings Summary must not:

- read `portfolio.db` (Portfolio Tracker's), import Portfolio Tracker ORM or service
  modules, or assume its filesystem layout;
- recreate provider reconciliation, deduplication, or Tax treatment inference;
- present a stale or partial provider read as current; or
- persist provider raw payloads beyond what artifact provenance requires.

Portfolio Tracker must not:

- read Earnings Summary's database, `v_thesis_status`, report HTML, or brief files;
- host thesis, valuation, research-alert, journal, coaching, or recommendation
  surfaces after cutover; or
- run scheduled CIO/coaching/Cockpit work after Phase 4.

The one-way Tier-A import of human-capital buckets from `CIO_CONTEXT.local.md`
(federation doc §3.2) is retired in Phase 3 when ownership of that context moves
here; until then it remains the supported stopgap.

---

## 7. Capability mapping

The unit of migration is the capability, absorbed into the Partner-PRD information
architecture. Parity is judged against the checklist in §7.1–§7.8, not against
Portfolio Tracker's pixels.

### 7.1 Cockpit / action queue → Today + Senior Partner Brief

- Signal detection is rebuilt as Earnings Summary read models: thesis/valuation/alert
  signals come from local stores (which Portfolio Tracker's cockpit was reading over
  the bridge anyway); portfolio signals (weight changes, drawdown, stale data) come
  from `/api/v1` analytics and data-quality endpoints.
- Delivery follows the ratified rule: the Senior Partner Brief owns delivery, the
  detection machinery owns detection; only tier-1 decisive alerts deliver
  immediately. The migrated queue must not reintroduce a parallel ping channel.
- Queue item state (accept/dismiss/snooze/execution) maps onto the existing action
  cores and, where an accepted item represents an owner choice, the Owner Decision
  model. Historical `action_queue` rows import as legacy provenance only.
- Ranking must respect the owner's standing weighting: thesis-health and
  valuation-driven signals rank far above concentration-driven signals.

### 7.2 Thesis health → Portfolio → Health

Already Earnings Summary data; the Portfolio Tracker page is a bridge-fed remote
view. Ensure Portfolio → Health covers what `ThesisHealth.tsx` showed (per-holding
`overall_status`, breach detail, evaluation age), then the remote page is redirected.

### 7.3 Trade/decision journal → Review → Ledger

- `trade_decisions` and `trade_tags` import into `decisions` (and tag vocabulary)
  under the Phase 3 migration contract (§9), with `decided_by='owner'` where the
  record is an owner action and provenance linkage otherwise.
- Post-cutover, all new journal writes occur here. Tracker-detected executed position
  changes continue to reconcile via `/api/v1/transactions`.
- Trade analysis, trade timeline, and scorecard presentation re-render locally from
  `/api/v1` analytics (`position-performance`, exit quality, performance) joined to
  the local journal — the join the federation doc already lists as a repair item.

### 7.4 CIO advisor + monthly briefs → Senior Partner Brief + Ask

- Capability-level parity: the proactive synthesis job of the monthly brief and the
  conversational job of the CIO chat are fulfilled by the Senior Partner Brief and
  Ask respectively. The Portfolio Tracker chat UI is not ported.
- The CIO persona and strategic directives (canonical in Portfolio Tracker's
  `CIO_CONTEXT.md`, with private overlay `CIO_CONTEXT.local.md`) transfer to Earnings
  Summary ownership in Phase 3: public rubric into this repository's context/anchor
  system, private overlay moved locally by the owner (gitignored; never committed,
  never logged).
- Historical `chat_sessions`/`chat_turns`/`monthly_briefs` import as read-only legacy
  provenance if the Phase 0 inventory deems them useful; otherwise they are archived
  with the Portfolio Tracker database backup.
- All advisory generation routes through `call_llm`/`call_llm_structured` with new
  purpose keys (§10); the Portfolio Tracker path that shelled out to `claude_cli.py`
  is not carried over.

### 7.5 Coaching → governor + coach pings

Portfolio Tracker's coaching-tip generation merges into the existing coach-ping and
governor machinery under the Partner PRD's notification policy (at most one
proactive ping per day; suppressed items flow into the brief). No second coaching
channel.

### 7.6 Human capital → owner_profile_facts

- `human_capital_overlap` records become affirmed `owner_profile_facts` (or a typed
  store behind the same propose/affirm gate), replacing the regex parse of
  `CIO_CONTEXT.local.md` as the source of human-capital bucket caps and members.
- Editing moves to the owner-context surfaces; interpretation (overlap warnings in
  recommendations) is Earnings Summary judgment grounded in provider Positioning
  facts.

### 7.7 Policy / posture → Portfolio Posture (owner intent only)

- Owner-intent weighting and posture records migrate into Portfolio Posture /
  `positioning_intents` / affirmed facts.
- Benchmark and calculation configuration remains a Portfolio Tracker API-owned
  setting (provider PRD §5.2); the Phase 0 inventory splits `policy_weights` rows
  between the two along exactly that line.

### 7.8 Portfolio dashboards → Portfolio → Allocation / Health / Record

User-facing Positioning, performance, risk, and benchmark presentation renders here
from `/api/v1` analytics with full envelope metadata. This is the same substrate P0
of the Partner PRD (Risk Budget history, Incremental Dollar Recommendation) consumes;
building the typed client first means P0 lands on the stable contract.

### 7.9 Surface and interaction requirements

- Surfaces are delivered with the existing stack: pipeline-rendered HTML, `src/ui/`
  tokens and controls, HTMX, the comments-server routes, Telegram, and the mobile
  Inbox. No new application framework.
- Owner display conventions carry over from the Portfolio Tracker UI: dollar amounts
  render as whole dollars (no cents; EPS, percentages, and share counts keep
  precision), and data tables the owner sorts today remain sortable on every column.
- Every provider-fact panel shows as-of date, provider coverage, and stale/partial
  warnings; Telegram redaction rules (no total portfolio value, no account balances,
  no tax-lot detail) apply to all migrated content.

---

## 8. API consumption requirements

### 8.1 Typed v1 client

Evolve `src/integrations/portfolio_tracker_client.py` into the single typed client
for `/api/v1`:

- strict Pydantic response models generated against the published OpenAPI/fixtures;
- Decimal-string and ISO-date parsing at the boundary; explicit fraction-versus-
  percent handling (retiring the `_f()` coercion hazard);
- the shared response envelope (`schema_version`, `as_of`, `generated_at`, coverage,
  `is_partial`, `is_stale`, warnings, methodology and version) surfaced to every
  caller, not swallowed;
- major-version incompatibility fails closed with a compatibility error;
- configurable base URL (no hard-coded port 8000 assumption beyond the default),
  loopback default, bounded tiered timeouts, `probe_tracker`-style health checks;
- the existing never-raise degradation contract (`available=False` + reason)
  retained; and
- telemetry limited to request ID, endpoint, duration, schema version, and sanitized
  status — never response bodies, holdings, balances, or account labels.

### 8.2 Endpoints consumed

`health`, `portfolio-snapshot`, `positions`, `position-snapshots`, `transactions`,
`securities`, `cash-flows`, `data-quality`, `sync-runs` (read-only),
`analytics/performance`, `analytics/position-performance`, `analytics/risk`,
`analytics/positioning`. Earnings Summary requests no consumer-specific endpoint;
gaps are raised as shared-contract issues in Phase 0/1 review.

Earnings Summary never calls operator mutation endpoints. When source data needs
repair or refresh, the UI links the owner to Portfolio Tracker's operations console.

### 8.3 Degradation

When the provider is unavailable or a response is stale/partial:

- surfaces render the last-valid artifact with explicit age and reason, or an
  explicit unavailability state — never a silently blended view;
- decision-grade outputs (Incremental Dollar Recommendation, position review,
  brief sections that depend on current portfolio facts) follow the Partner PRD's
  freshness gates: stale required inputs block or downgrade the output visibly; and
- scheduled jobs degrade per-item and log an explicit failed-ingestion event.

---

## 9. State migration into Earnings Summary

### 9.1 Inventory (Phase 0)

Classify every Portfolio Tracker record class listed in §4.3 as: migrate-active
(becomes a live object here), import-provenance (read-only legacy artifact), or
archive-only (retained in Portfolio Tracker's backup, not imported).

Default dispositions, to be ratified:

| Source | Default disposition |
| --- | --- |
| `trade_decisions`, `trade_tags` | Migrate-active into `decisions` + tag vocabulary |
| `action_queue` history | Import-provenance |
| `chat_sessions`, `chat_turns` | Archive-only unless the inventory finds decision-relevant threads |
| `monthly_briefs` (+ jobs) | Import-provenance (metadata + rendered text) |
| `human_capital_overlap` | Migrate-active into owner-context stores |
| `policy_weights` | Split: owner intent migrates; calculation config stays |
| `earnings_calendar` | Not migrated; superseded by this repository's earnings data |

### 9.2 Migration contract

Every imported dataset follows the provider PRD §10.2 contract: source/destination
natural keys, field mapping, owner/source attribution, timestamp semantics,
null/default policy, source-system and source-row identifiers, idempotency key,
validation queries, quarantine policy, and rollback procedure. Imports are
idempotent; re-running does not duplicate rows.

Journal-specific rules:

- a Portfolio Tracker decision that matches an existing `decisions` row (same
  ticker, action, and date within tolerance) reconciles rather than duplicates, with
  the conflict logged for review;
- imported advisor-generated records must not inflate the Owner Decision default
  view — they land as advice artifacts or provenance per the owner-first journal
  rules (Partner PRD §9.3);
- `process_quality` is never fabricated on import; and
- original Portfolio Tracker identifiers are preserved for audit.

### 9.3 Live-data safety

Before any import or destructive step, both databases follow the standard sequence:
verified backup (restore-tested without printing holdings), preview of affected
tables/date ranges/row counts, explicit owner approval, idempotent run, source/
destination count and invariant comparison, and a read-only source through the
agreed rollback window. Alembic owns all schema changes here; no inline DDL.

---

## 10. LLM architecture and governance

- New or changed purposes introduced by absorbed capabilities register in
  `src/llm/cli.py::LLM_MODELS` and `src/llm/prompt_versions.py`. Expected keys
  (final names at implementation): an action-queue/signal synthesis purpose if any
  LLM ranking survives (prefer deterministic ranking), and the already-planned
  `senior_partner_brief`, `incremental_dollar_recommendation`,
  `investment_decision_card`, `decision_draft_parse` from the Partner PRD. The CIO
  advisor does not get a standalone purpose; its jobs land under the brief and Ask
  purposes.
- Model IDs are not selected in this PRD; every purpose enters the model-picker/eval
  loop and earns the cheapest-at-parity model.
- Each purpose gets a monthly budget row with explicit `warn`/`skip`/`block`
  behavior; every call logs purpose, prompt version, model, tokens, cost estimate,
  latency, outcome, and artifact linkage in the existing ledgers.
- Structured outputs validate against Pydantic schemas with the repair-once rule; a
  second failure records a failed call and preserves the prior valid artifact.
- Scheduled LLM work respects the protected 03:00–05:00 America/Los_Angeles pipeline
  window and registers cadence in `directives/llm_quota_scheduling.md` only with
  separate owner authorization.
- Deterministic portfolio math is never delegated to the LLM, and prompt context
  containing provider facts always carries its as-of metadata.

---

## 11. Cross-repository dependency contract

| Dependency | Provider | Consumer | Blocking deliverable |
| --- | --- | --- | --- |
| Bulk current state | Portfolio Tracker | Earnings Summary | `/api/v1/portfolio-snapshot` + fixtures |
| Positions / transactions / cash flows | Portfolio Tracker | Earnings Summary | v1 resources with cursor pagination and correction metadata |
| Performance / position alpha / risk / Positioning | Portfolio Tracker | Earnings Summary | Versioned methodology, units, envelope semantics, parity tests |
| Exit-quality facts | Portfolio Tracker | Earnings Summary | v1 analytics parity with the current endpoint the client consumes |
| Sync health / data quality | Portfolio Tracker | Earnings Summary | health, sync-runs, data-quality v1 resources |
| Contract compatibility | Portfolio Tracker | Earnings Summary + Wealthplan | OpenAPI artifact + shared sanitized fixture suite |
| Journal export | Portfolio Tracker | Earnings Summary | Read-only export/fixtures for `trade_decisions`/`trade_tags` + approved mapping |
| Advisory history export | Portfolio Tracker | Earnings Summary | Inventory-approved provenance export |
| Owner-context transfer | Portfolio Tracker | Earnings Summary | CIO persona/context re-homing; human-capital records |
| Surface parity sign-off | Earnings Summary | Portfolio Tracker | Per-capability acceptance gates authorizing legacy removal |
| Bridge removal | Portfolio Tracker | Both | Deletion of `services/earnings_summary.py` and its routes after all consumers migrate |

Earnings Summary client and read-model work can proceed against documentation and
fixtures alone. No Portfolio Tracker removal proceeds until the matching Earnings
Summary gate passes.

---

## 12. Delivery sequence

Phase identifiers are shared with the companion PRDs. Earnings Summary work within a
phase may parallelize with the Partner PRD's P0–P3 tracks; the interleaving rule is:
consolidation supplies the provider contract and migrated substrate, the Partner
program supplies product behavior on top of it.

### Phase 0 — Ratify boundary and inventory

- Approve the ownership matrix (§6) and capability mapping (§7).
- Inventory every bridge consumer in Portfolio Tracker and every
  `portfolio_tracker_client` call site here; classify each against the v1 contract.
- Review the shared v1 fields and fixtures Earnings Summary depends on (envelope,
  analytics units, exit quality, transactions).
- Ratify the §9.1 migration dispositions and the journal reconciliation rule.
- Decide the open decisions in §16.

Exit gate: all three PRDs share one ownership and phase map; every migrating
capability has a named destination system; no disputed capability remains.

### Phase 1 — Typed client and fixtures

- Build the typed v1 client (§8.1) against published OpenAPI and sanitized fixtures;
  add contract tests that run without a live provider or live data.
- Keep the legacy client paths working unchanged.

Exit gate: client implemented and tested from documentation and fixtures only;
incompatible contracts fail closed.

### Phase 2 — Consumer adoption

- Move every live portfolio read (`advisor`, `allocation`, `ask`, `attribution`,
  `calibration`, dashboards) onto the typed v1 client behind a temporary switch;
  dual-read where outputs are decision-grade, comparing sanitized aggregates within
  documented tolerances.
- Land the analytics joins (exit quality, position alpha) the migrated Review
  surfaces need.

Exit gate: no supported Earnings Summary path uses legacy endpoints or ad hoc
coercion; parity within tolerances; owner approves cutover.

### Phase 3 — Journal and owner-state migration

- Import `trade_decisions`/`trade_tags` under the §9.2 contract; freeze legacy
  journal mutations in Portfolio Tracker at cutover.
- Migrate human-capital records and owner-intent posture; transfer CIO
  persona/context ownership; retire the `CIO_CONTEXT.local.md` regex import.
- Import approved provenance artifacts.

Exit gate: counts, keys, and sampled histories reconcile; new journal writes occur
only here; owner-first ledger view is not polluted by imports; rollback rehearsed.

### Phase 4 — Surface cutover

- Ship capability parity per §7: Health thesis coverage, Review ledger/analysis/
  timeline/scorecard, action-queue absorption into Today/brief, brief+Ask advisory
  parity, coaching merge, posture and human-capital surfaces.
- Portfolio Tracker replaces migrated navigation with redirects/links and stops
  scheduled CIO/coaching/Cockpit work.
- Update this repository's `cron/` fleet for any new refresh legs (backup and
  preview scheduled-task changes; respect the shared run lock).

Exit gate: production-like acceptance passes per capability; stale/offline behavior
explicit; owner approves Earnings Summary as the default investing interface.

### Phase 5 — Legacy retirement

- Portfolio Tracker deletes `services/earnings_summary.py`, its routes, the migrated
  services/tables/pages, per its PRD.
- Earnings Summary removes the migration switch, legacy client paths, and any
  remaining references to Portfolio Tracker analysis pages.

Exit gate: repository-wide scans find no bridge, no legacy client path, no
cross-database read in either direction; rollback window expired with approval.

### Phase 6 — Hardening

- Run this repository's contract fixture suite against the same provider artifact
  Wealthplan uses; fail CI on incompatible drift.
- Test provider-offline, stale, partial, and version-mismatch behavior end to end;
  verify no balances/holdings in logs, fixtures, or git.
- Measure local latency of the composed pages against the bulk-snapshot budget.

---

## 13. Acceptance criteria

### 13.1 Boundary

- No Earnings Summary code reads Portfolio Tracker's database or imports its
  modules; no Portfolio Tracker code reads this repository's database or files.
- All live portfolio facts arrive via the typed v1 client; envelope metadata reaches
  every rendering surface.
- Earnings Summary holds no provider mutation authority.

### 13.2 Capability parity

- Every §7 capability passes its acceptance checklist before the corresponding
  Portfolio Tracker surface is removed.
- The action queue delivers through the brief/governor under the notification caps;
  no second ping channel exists.
- Review → Ledger defaults to Owner Decisions; imported records respect that rule.

### 13.3 Data correctness and honesty

- No failed or partial provider read renders as current; last-valid artifacts show
  age and reason.
- Units, currency, and dates are explicit end to end; whole-dollar display holds.
- Imported journal rows reconcile by count and key; no duplicates; no fabricated
  `process_quality`.

### 13.4 Governance and safety

- Every migrated LLM capability has a purpose key, budget row, structured schema,
  logged costs, and an eval entry.
- No credentials, balances, holdings, account labels, or raw provider payloads in
  logs, fixtures, errors, or git; `CIO_CONTEXT.local.md` and successors remain
  gitignored.
- Every destructive step had a verified backup, approved preview, and rehearsed
  rollback.

---

## 14. Testing and verification

- Client contract tests over shared sanitized fixtures (same artifact as
  Wealthplan): envelope, units, pagination, stale/partial/failure matrices,
  major-version rejection.
- Dual-read parity harness emitting only pass/fail, counts, dates, and normalized
  deltas — never balances or account labels.
- Journal import tests: idempotency, reconciliation-not-duplication, provenance
  fields, owner-first view integrity, rollback.
- Read-model tests for absorbed signals (thesis/valuation/portfolio/data-quality)
  including degraded-input cases.
- Governance tests: purpose registration, budget behavior, structured-output
  repair-once, protected-window scheduling.
- Manual acceptance: start the provider and this system in either order; exercise
  each migrated surface current and degraded; verify Telegram redaction; verify the
  operations-console handoff links.

---

## 15. Failure modes

| Failure | Required behavior |
| --- | --- |
| Provider offline | Surfaces show explicitly aged last-valid artifacts or unavailability; decision-grade outputs block or downgrade visibly |
| Partial provider coverage | Never blended with prior data field-by-field; one coherent snapshot with warnings |
| Provider schema mismatch | Client fails closed with a compatibility error |
| Journal import conflict | Quarantine and report; never silently overwrite either record |
| LLM failure/budget block | Deterministic content renders; prior valid artifact retained; failure surfaced |
| Legacy surface removed before parity | Blocked by §11 gates |
| Bridge removal breaks a forgotten consumer | Phase 0 inventory + repo-wide scan gate removal |

---

## 16. Open implementation decisions (close in Phase 0)

1. Final disposition of CIO chat history and monthly briefs (import-provenance
   versus archive-only), and where imported provenance renders (Portfolio → Record).
2. Whether human-capital facts live directly in `owner_profile_facts` or a typed
   store behind the same propose/affirm gate.
3. The exact split of `policy_weights` rows into owner intent versus calculation
   configuration.
4. Journal reconciliation tolerance (date window, action matching) for
   `trade_decisions` import.
5. Whether the absorbed action-queue state machine reuses `alerts`/standup state or
   a dedicated table.
6. Whether Portfolio Tracker's holdings-page research enrichment (tracked flag, next
   earnings, thesis status) is dropped or replaced by a link into this system.
7. Service discovery for the provider (fixed default port with env override, or a
   registry) — shared with the other PRDs.
8. Rollback-window lengths for journal cutover and bridge removal — shared decision.

---

## 16.1 Phase 3-5 execution record (2026-07-23, owner-authorized)

The owner authorized M4-M6 with migrate-by-judgment on 2026-07-23. Verified
backups were taken first (tracker: `backups/portfolio_2026-07-23.db` via its
backup job; this repo: `data/portfolio.db.pre-m4-journal-import.bak`,
integrity ok). The tracker's journal proved nearly empty, so the migration
resolved as:

| Source (rows) | Disposition | Rationale |
| --- | --- | --- |
| `trade_decisions` (0) | Nothing to migrate | Empty |
| `trade_tags` (1) | **Migrated** → `analyst_notes` id 62 (`source_ref=portfolio-tracker:trade_tags:1`, idempotent via `execution/import_tracker_journal.py`) | The CPNG `bought_with_no_thesis` lesson is a genuine process-quality unit |
| `action_queue` (29: 19 resolved / 9 snoozed / 1 dismissed) | Omitted; archive-only in the tracker backup | Stale operational signal history; underlying thesis/valuation data lives here already |
| `chat_sessions`/`chat_turns` (1/9) | Omitted; archive-only | One low-stakes CIO chat |
| `monthly_briefs` (2) | Omitted; archive-only | Rendered HTML artifacts; superseded by this repo's advisory briefs |
| `human_capital_overlap` (30) | **Omitted deliberately** | The same buckets were imported earlier from `CIO_CONTEXT.local.md` and ratification-REJECTED in `owner_profile_facts`; importing again would override an explicit owner ruling |
| `policy_weights` (4) | Stays in the tracker | Benchmark calculation configuration (Phase-0 ruling PT-6) |

Phase-4 actions taken: the tracker's "monthly brief email" scheduled task was
disabled (briefing is this repo's capability; re-enable with
`Enable-ScheduledTask` if rollback is needed). CIO persona re-homing is
deferred to the Senior Partner Brief build (Partner PRD P2) — until that
surface exists there is nothing to re-home into.

Phase-5/6 state: the tracker's ES-bridge deletion and analysis-surface
removal remain **gated on this repo building the replacement surfaces**
(Partner PRD P0-P2). Deleting them today would remove working daily
workflows with no replacement, which the ratified plan forbids. The
tracker-side freeze of journal mutation endpoints rides with that same
Phase-5 PR.

## 17. Definition of done

The consolidation is complete when:

1. Earnings Summary is the owner's default investing interface, covering analysis,
   allocation, risk interpretation, journaling, learning, and proactive advice.
2. All live portfolio facts arrive through the documented `/api/v1` contract via one
   typed client, with envelope metadata surfaced everywhere.
3. The owner's Portfolio Tracker journal history lives in the `decisions` ledger
   with verified counts and provenance; new journal writes occur only here.
4. The CIO persona, human-capital context, and owner-intent posture are owned here;
   the regex import from `CIO_CONTEXT.local.md` is retired.
5. Portfolio Tracker contains no reader of this repository's data and no active
   analysis, journal, coaching, or recommendation surface; its bridge module and
   migrated tables are removed per its own PRD gates.
6. Every migrated LLM capability is governed (purpose, budget, schema, evals, cost
   logging).
7. Wealthplan and Earnings Summary consume the same provider API version and fixture
   suite.
8. All tests in §14 pass; no private data appears in logs, fixtures, or git; every
   destructive step had backup, preview, approval, and rollback.

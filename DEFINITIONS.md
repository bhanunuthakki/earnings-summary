# Definitions

**Scope:** project
**Owner:** earnings-summary
**Inherits:** the harness-loaded global `DEFINITIONS.md`; no host-specific path is authoritative.

Canonical terminology for this project. Use these terms verbatim at durable
boundaries: code and schema identifiers, APIs, persisted state, canonical UI and
instruction contracts, and shipped operational documentation. Exploration may use
clearly marked provisional wording; define or reconcile a term here (or in the
inherited global vocabulary) before it crosses one of those durable boundaries.

## Logical Idempotency Key

**Definition.** The stable business identity of the effect a directive intends to
produce. Repeating an authorized operation with the same Logical Idempotency Key
must not create a second logical deliverable or duplicate state transition.
**Not to be confused with.** Content Identity, which identifies exact bytes;
Observation Version, which distinguishes source states over time; or Attempt
Identity, which identifies one execution and therefore changes on retry.

## Content Identity

**Definition.** A digest, normally SHA-256, of exact artifact bytes or canonicalized
content. Equal Content Identity proves equal content, not equal business purpose or
source observation time.
**Not to be confused with.** A Logical Idempotency Key. Different bytes may be new
versions of the same logical deliverable; identical bytes may be observed by
different attempts.

## Observation Version

**Definition.** The append-only identity of what a source exposed at a particular
source or knowledge time. A changed upstream payload creates a new Observation
Version even when its Logical Idempotency Key is unchanged.
**Not to be confused with.** Fetch time alone. Preserve the source timestamp or
filing identity when available, the fetched-at knowledge time, and Content Identity.

## Source Inventory Presence

**Definition.** The explicit agreement state for one document or attachment across
the authoritative inventory surfaces used to enumerate it. Its values are
`matched` when every required surface lists the item, `index_only` when only the
archive directory lists it, and `manifest_only` when only the filing manifest
lists it. Metadata absent from one surface remains unknown rather than inferred.
**Lives in.** `SecFilingPackageAttachment` and
`src/filings/sec_filing_package_inventory.py`.
**Not to be confused with.** Source coverage or acquisition completeness. Source
Inventory Presence preserves an authority disagreement for one item; the enclosing
inventory still separately proves whether every required authority response was
captured and reconciled.

## Attachment Locator Status

**Definition.** The SEC authority's ability to identify a fetchable locator for one
declared filing-package attachment. Its values are `available` when both an exact
filename and SEC archive URL are present, and `authority_omitted` when the SEC
directory and filing manifest declare the same attachment in matching authority
order and byte size but publish neither filename nor fetchable document URL.
**Lives in.** `SecFilingPackageAttachment.locator_status` and the corresponding
expected-document coverage reason details.
**Not to be confused with.** Source Inventory Presence. An authority-omitted
attachment may be `matched` across both inventory surfaces while remaining
unfetchable; consumers requiring document bytes must exclude it and retain the
explicit `authority_unavailable` coverage disposition.

## Attempt Identity

**Definition.** The unique identity of one execution attempt, including retries.
Attempt Identity supports logs, checkpoints, cost attribution, and failure recovery;
it must never be used as the Logical Idempotency Key because it changes on every run.

## DCF Debt Scope

**Definition.** The exact liability set included in a DCF equity bridge. Its value
is either `interest_bearing_debt_only`, which excludes lease liabilities, or
`debt_and_lease_obligations`, which includes both debt and lease liabilities.
**Contract.** Every governed equity bridge states its DCF Debt Scope explicitly;
component selection and provenance must reconcile to that scope without silently
mixing the two sets.
**Not to be confused with.** `total_debt_basis`, which records how the selected
amount was resolved from an aggregate or component source. DCF Debt Scope defines
what belongs in the amount; `total_debt_basis` explains how that amount was found.

## Research Level

**Definition.** The evidence depth authorized for an active tracked instrument. The four levels are mutually exclusive and collectively exhaustive: `catalog` preserves identity and raw-source availability; `screened` adds compact deterministic screening metrics; `monitored` adds narrow company-specific monitoring; `governed` admits the company to the complete document, fact, provenance, brief, DCF, and research-artifact contract.
**Derivation.** Research Level is derived vocabulary, not a new database column: active `portfolio` and `evaluation` rows are governed; active `watchlist` rows are monitored; active `index_member` rows are screened; active `none` rows are catalog. An archived row has no active Research Level and authorizes no scheduled work.
**Not to be confused with.** Coverage Role (`list_type`) records the owner's relationship to the name; Schedule Class is derived from Coverage Role and controls cadence; Instrument Kind (`instrument_type`) describes the security; Lifecycle (`archived_at`) controls whether any active work is allowed. Legacy `list_type='etf'` is compatibility debt; ETF is an Instrument Kind, not a Coverage Role.

## Tracked Instrument State

Every tracked-company row is described by four independent axes. Together they are MECE and no fifth persisted routing axis is required.

- **Coverage Role:** exactly one of `portfolio`, `evaluation`, `watchlist`, `index_member`, or `none`. Legacy `etf` rows remain readable only until normalized.
- **Lifecycle:** `active` when `archived_at IS NULL`; otherwise `archived`. Lifecycle is orthogonal to the preserved Coverage Role.
- **Instrument Kind:** equity, ETF, or another security classification stored separately from Coverage Role.
- **Schedule Class:** derived from Coverage Role: portfolio is `P1`; evaluation and watchlist are `P2`; index_member, none, and legacy etf are `P3`.

The database row is the sole membership authority. Workbooks, research directories, thesis files, cached LLM artifacts, and WACC seeds are outputs or inputs; they never create, restore, or upgrade membership.

## Research Task

**Definition.** A staged, owner-visible request for a bounded research pass. A task
may carry `estimated_cost_usd`, the pre-run estimate used for approval and queue
presentation, and `task_metadata_json`, a small lifecycle metadata object such as
the session prompt and packet timestamps.
**Not to be confused with.** Realized provider or LLM spend, which belongs in the
corresponding source-cost or LLM-call ledger; or Attempt Identity, which identifies
one execution. Research-task metadata is not a run identifier and must not be
stored in or exposed as one.

## Filing Regime

**Definition.** The regulatory framework governing an issuer's SEC periodic reporting obligations and filing document formats. The canonical regimes are:
- `10-K`: Domestic US operating companies and issuers filing annual Form 10-K, quarterly Form 10-Q, and current Form 8-K.
- `20-F`: Foreign Private Issuers (FPIs) filing annual Form 20-F and furnishing interim/quarterly releases and updates on Form 6-K.
- `40-F`: Canadian Multijurisdictional Disclosure System (MJDS) filers filing annual Form 40-F and furnishing interim reports on Form 6-K.
- `none`: Non-SEC registrants or unsponsored ADRs with no direct EDGAR reporting obligation (e.g. `NTDOY`, `FLKR`).

**Contract.** Pipelines branch deterministically on `tracked_companies.filing_regime`. Interim quarters for `20-F`/`40-F` issuers MUST route through the 6-K exhibit ingestion pipeline rather than assuming US 10-Q XBRL availability.
`NULL` means the regime has not yet been resolved; it is not equivalent to the
known `none` regime. Until `none` is round-trippable through the typed company
model and every filing router, persisted `none` values remain compatibility debt.
The quarterly-segment router returns an explicit unsupported state for unresolved
rows. The older `Company.interim_doc_type` and `Company.annual_doc_type` fallback
heuristics remain compatibility debt and must not be interpreted as a resolved
Filing Regime.

## Foreign Private Issuer (FPI)

**Definition.** An SEC-registered non-US entity that qualifies under Exchange Act Rule 3b-4 to file annual reports on Form 20-F and furnish quarterly/interim financial statements, press releases, and material updates on Form 6-K instead of domestic 10-Q/10-K/8-K forms.

**Contract.** FPIs do not publish structured XBRL in the SEC `/api/xbrl/companyfacts/` endpoint for quarterly 6-Ks. All quarterly/interim financials for FPIs MUST be extracted from primary 6-K exhibit HTML/text and registered with evidence ledger provenance.

## Interim Filing Obligation

**Definition.** The expected quarterly or half-yearly SEC filing type derived from an issuer's Filing Regime: Form 10-Q for `10-K` filers, and Form 6-K (earnings release / financial statements exhibit) for `20-F` and `40-F` filers.


## Coverage Role Resource Contract

| Coverage Role | Research Level | Schedule | Deterministic work | LLM / deliverables |
|---|---|---|---|---|
| `portfolio` | governed | P1 daily and event-driven | full data and trigger processing | full brief, DCF, advisor, daily lenses; automatic pre/post-earnings |
| `evaluation` | governed | P2 weekly and event-driven inputs | full governed data refresh | full brief, DCF, decision card, weekly lenses; pre/post-earnings only when explicitly enabled/requested |
| `watchlist` | monitored | P2 weekly | narrow monitoring inputs | narrow weekly lenses only; no full brief, DCF, advisor candidate, or dirty-queue rebuild |
| `index_member` | screened | P3 monthly | compact deterministic screening | no scheduled company-specific LLM work |
| `none` | catalog | P3 metadata only | no company-specific scheduled work | none |
| legacy `etf` | compatibility | P3 | compatibility reads only | none |
| any archived row | none | none | none | none |

Cron invariants: every selector excludes archived rows; a cadence runs only its exact tier; target ages are P1 daily, P2 seven days, and P3 thirty days. Full briefs, DCFs, dirty-artifact drains, model evaluation, and advisor candidate generation are restricted to active portfolio/evaluation rows. P3 work is deterministic and zero-token. These rules require neither a new migration nor a recurring parity checker.

## Data Collection Policy

**Definition.** The owner-approved ceiling on which source and artifact combinations may be collected for each Coverage Role. It governs authorization and depth; it does not report current freshness, pipeline health, backlog, or failures.
**Contract.** Portfolio permits automatic full collection. Evaluation permits metadata automatically and full documents only for an explicit owner request or approved event rule. Watchlist permits narrow metadata monitoring. Index member permits only deterministic FMP screening facts. Catalog and legacy ETF roles authorize no new company-specific collection. Webcasts are excluded for every role.

## Issuer Acquisition Policy

**Definition.** The immutable, canonical-JSON-hashed set of issuer-specific SEC, investor-relations, FMP normalization, and transcript rules. The canonical issuer identity is the key; ticker symbols are aliases only.
**Contract.** Acquisition plans, source captures, extraction derivatives, and admission receipts carry the exact policy hash used. A policy change may invalidate a derivative or plan, but it never rewrites or deletes preserved source bytes.

## Provider Circuit

**Definition.** The durable provider-wide admission state that prevents repeated calls when a source is unavailable. Its states are `CLOSED`, `OPEN`, and `HALF_OPEN`.
**Contract.** `CLOSED` permits bounded live work. `OPEN` permits no provider calls and schedules a future probe. `HALF_OPEN` grants exactly one leased entitlement probe. Missing or invalid authentication and account-wide authorization failures open immediately; configured consecutive rate-limit or provider failures may open at their threshold. Endpoint-specific denial does not poison the Provider Circuit.

## FMP Recovery Work

**Definition.** One idempotent desired FMP refresh, identified by ticker, endpoint, period, freshness generation, and Issuer Acquisition Policy hash. Its durable states are `PENDING`, `LEASED`, `SATISFIED`, and `TERMINAL`.
**Contract.** A Work Lease grants bounded ownership across an external operation, while every database claim or outcome transaction remains short. Expired leases are reclaimable, and stale lease tokens cannot complete work. Portfolio work outranks owner-requested Evaluation work, which outranks permitted Index-member screening work.

## Corpus Mode

**Definition.** A zero-FMP-network recovery mode that indexes and extracts already-preserved raw FMP files while live provider access is unavailable.
**Contract.** Corpus Mode never modifies raw corpus bytes, advances provider freshness, or claims that stale data was refreshed. Unrefreshed work remains durable FMP Recovery Work. An alternative source may satisfy a work item only with explicit period-and-concept coverage, freshness, provenance, policy authorization, and no unresolved disagreement.

## Refresh Receipt

**Definition.** The typed, attributable outcome of a refresh run. Its status is exactly one of `FRESH`, `DEGRADED_CORPUS`, `PARTIAL`, or `FAILED`.
**Contract.** `FRESH` requires current-source obligations to be satisfied. `DEGRADED_CORPUS` means usable preserved data was rehydrated but refresh work remains. `PARTIAL` reports mixed usable and unresolved outcomes. `FAILED` reports no usable result. Receipts expose counts, backlog age, circuit state, and next probe without raw provider bodies or secrets.

## Coverage Lifecycle Actions

| Owner action | Result | Resource consequence |
|---|---|---|
| Add / Watch | active `watchlist`, P2 (except an actual portfolio holding remains portfolio) | monitored work only; no DCF or full brief |
| Build Evaluation | active `evaluation`, P2, `brief_dirty=1` | governed build becomes eligible |
| Position Detected | active `portfolio`, P1, `brief_dirty=1` | daily governed coverage becomes eligible |
| Position Exited | active `evaluation`, P2, `brief_dirty=1` | daily portfolio work stops; governed evaluation coverage remains |
| Evaluation Watch | active `watchlist`, P2, `brief_dirty=0` | full governed work stops; monitoring remains |
| Pass / Remove | archived, prior role preserved, `brief_dirty=0` | all scheduled work stops; raw pulls, provenance, and history remain |
| Restore | active with preserved role and derived tier | governed roles are queued; monitored/screened roles are not |
| Hard delete | exceptional membership-row removal only | never the normal lifecycle action and never implies raw-history deletion |

Precise verbs matter: **Build Evaluation** moves a watchlist, index, or catalog name to evaluation; **Position Detected** moves it to portfolio; the legacy Investment Decision Card transport verb `promote` records its decision while retaining evaluation coverage; discovery dismissal changes queue state only unless a separate coverage action is invoked.

## Thesis Evaluation Episode

**Definition.** One owner-facing assessment of one ticker under one materially distinct thesis, rule set, accepted evidence set, evaluator semantic version, and deterministic result. Re-running the evaluator with the same semantic inputs checks the same episode; it does not create another learning event.
**Lives in.** `thesis_evaluation_episodes`, with immutable raw-run membership in `thesis_evaluation_episode_members` and idempotent execution evidence in `thesis_evaluation_episode_check_receipts`.
**Not to be confused with.** A raw `thesis_evaluations` row or pipeline run. Those preserve executions; the Thesis Evaluation Episode is the deduplicated analytical unit shown to the owner.

## Evidence Acknowledgement

**Definition.** The owner's episode-specific statement that the evidence in one Thesis Evaluation Episode has been reviewed. It suppresses repeated action prompting for that episode while preserving its analytical warning and provenance.
**Not to be confused with.** Dismissing an alert, marking a Coach Ping delivered, or changing the company thesis. Those may delegate to or accompany an Evidence Acknowledgement, but none is equivalent by itself.

## Owner Decision Checkpoint

**Definition.** The immutable, canonical-hash-committed point-in-time payload the owner confirms before one consequential portfolio decision is persisted. It freezes the proposed action, holdings basis and availability, thesis state, alternatives, source event, and provenance needed to reconstruct the decision later.
**Not to be confused with.** An unconfirmed decision draft, a brokerage order, or a mutable latest holdings snapshot. Confirmation may atomically create an Owner Decision and sizing intent, but the checkpoint never executes a trade.

## CANONICAL ACTIONS

These are the owner-facing verbs that are allowed to mutate durable state. A label names the consequence, not merely the gesture: prefer **Applied — thesis updated** over **Done** or **Saved**. Every persistent action surface owes the same feedback contract: call `CCAction.busy(...)` immediately, call `CCAction.release(...)` on failure while retaining the actionable item, and call `CCAction.receipt(...)` on success before any `CCAction.leave(...)` removal. An endpoint response is not, by itself, a visible receipt.

### Approve / Dismiss

**Mutates.** `approve` accepts the specific proposal in front of the owner; `dismiss` closes or cancels it without accepting it. The noun must always accompany the verb in code and UI because **Approve has three existing semantic families**: queued-action approval executes/applies the queued mutation, positioning approval appends the submitted positioning intent, and research/Tenet proposal approval promotes a proposal (and may apply a saved view or supersede a prior Tenet). Do not introduce an unqualified `approve()` core that conflates them.
**Reversibility.** Queued-action dismiss is **undoable** through uncancel; research/Tenet and positioning approvals are **append-only** or superseded by a later revision; an applied queued action is **one-way** unless that action defines its own compensating operation. Proposal rejection/dismissal is **one-way** unless its owning workflow exposes reopen.
**Feedback owed.** Busy labels name the object (`Applying thesis change…`, `Dismissing alert…`); receipts name the durable consequence (`Applied — thesis updated`, `Dismissed — action cancelled`, `Adopted — Tenet is current`). Failures release the original control and retain the proposal.
**Surfaces.** Governance → Actions and holding alert rails; Positioning; Research proposals and saved views; Worldview Tenet proposals.

### Confirm / Correct / Defer

**Mutates.** `confirm` turns a decision draft into an Owner Decision using the parsed values; `correct` does the same with owner-edited values; both mark the draft terminal. `defer` performs no server write today and only hides/dims the draft for the current client session.
**Reversibility.** Confirm and Correct are **append-only** decision provenance plus a **one-way** terminal draft transition; later corrections must be new attributable revisions, not silent rewrites. Defer is **undoable** by refreshing or reopening the session because it is not durable.
**Feedback owed.** `Confirming decision…` / `Applying correction…`, followed by `Recorded — Owner Decision created`; on failure release the draft unchanged. Defer must say `Deferred — this session only`, never imply durable storage.
**Surfaces.** Mobile Inbox, the desktop Ledger decision-draft queue, and Telegram dispatchers that share the decision-draft action core.

### Ratify / Rewrite / Drop

**Mutates.** These reconcile a proposed decision falsifier: `ratify` accepts it and queues or completes tripwire arming; `rewrite` stores the owner's replacement falsifier; `drop` removes the proposed falsifier so no tripwire watches it. Use **Rewrite** as the owner-facing verb even where a transport payload still uses `edit`.
**Reversibility.** Ratify and Rewrite are **append-only** decision/falsifier provenance and are superseded by another explicit reconciliation; Drop is a **one-way** removal from the pending proposal, with a later falsifier requiring a new proposal.
**Feedback owed.** Receipts distinguish immediate from deferred effects: `Armed — tripwire is watching`, `Ratified — queued for arming`, `Rewritten — falsifier updated`, or `Dropped — no tripwire will watch this decision`.
**Surfaces.** Ledger decision reconciliation and any mobile/Telegram presentation of the same pending-falsifier queue.

### Affirm / Reject / Reaffirm / Retire / Update

**Mutates.** These govern proposed owner-profile facts: `affirm` promotes a proposed fact; `reject` closes a proposed fact without adopting it; `reaffirm` refreshes the evidence that an affirmed fact still holds; `retire` ends an affirmed fact; `update` appends a replacement proposal that supersedes the prior narrative rather than rewriting history.
**Reversibility.** Affirm, Reaffirm, and Update are **append-only** provenance; Reject and Retire are **one-way** status transitions. A changed belief is represented by Update or a new proposal, not by erasing the prior fact.
**Feedback owed.** Use consequence-first receipts such as `Affirmed — profile fact is active`, `Rejected — proposal closed`, `Reaffirmed — current as of today`, `Retired — fact no longer active`, and `Proposed — update awaiting review`.
**Surfaces.** Worldview/Profile governance, Ledger review queues, and weekly accountability packets that link back to the canonical action surface.

### Adopt

**Mutates.** `adopt` records that the owner accepts a derived recommendation or proposed Tenet into durable owner-governed state. For allocation recommendations, the endpoint may expose the more precise dispositions `save_intent`, `hold_accountable`, or `dismiss`; do not relabel those distinct consequences as a generic Adopt button.
**Reversibility.** Adoption is **append-only**: later owner intent or a later Tenet may supersede it. A workflow may offer an explicit revert/retire action, but adoption itself never deletes its source proposal or provenance.
**Feedback owed.** The receipt names what became durable (`Saved — allocation intent recorded`, `Adopted — Tenet is current`) and preserves the source link. Failure releases the proposal without changing its displayed status.
**Surfaces.** Portfolio → Allocation recommendations and Worldview Tenet review/auto-adoption receipts.

### Save / Discuss / Incorporate

**Mutates.** These are the On My Mind ladder verbs. `save` patches the note's ladder state to saved-for-later; `discuss` patches it to discuss and opens the appropriate web thread; `incorporate` find-or-creates an inert proposed research task and marks the note incorporated. None of them fetches, calls an LLM, or publishes a research artifact. `dismiss` belongs to Approve / Dismiss and archives the note.
**Reversibility.** Save and Discuss are **undoable** by a later ladder choice; Incorporate is **append-only** because the staged task retains provenance and must be disposed of in its own workflow.
**Feedback owed.** Use `Saved — revisit later`, `Discuss — opening the ticker thread`, or `Queued — proposed research task created`; do not say research was completed. A failed handoff releases the note in place.
**Surfaces.** On My Mind on the dashboard, ticker-scoped feed cards, and Telegram presentations of the same ladder.

### Resolve / Archive / Route

**Mutates.** These are journal-note lifecycle verbs. `resolve` marks an open note answered/done, optionally with a resolution note; `archive` marks it no longer relevant; `route` rewrites a triage note's kind and intent so it leaves the triage queue, with best-effort reconciliation to its source comment.
**Reversibility.** Archive is **undoable** through Unarchive. Route is **undoable** only through another explicit reclassification/routing action. Resolve is a **one-way** lifecycle transition; a renewed question is a new or superseding note, not a silent reopen.
**Feedback owed.** Receipts state `Resolved — journal item closed`, `Archived — Undo`, or `Routed — now a watch item` (naming the selected destination). Failures restore the control and leave the row visible.
**Surfaces.** Research → Journal, Companies → Triage, Home/holding inboxes, and comment-backed report views.

### Queue / Watch / Build / Pass

**Mutates.** These govern discovery and investment-decision candidates. `queue` moves a discovery candidate to the owner-approved build queue but does not build it; `watch` idempotently adds or restores its ticker as active watchlist coverage without changing candidate status; `build` starts the bounded build pathway and upgrades the ticker to active evaluation coverage, while its worker alone writes `building`/`built` and artifacts; `pass` records an avoid decision with reason/revisit conditions when supplied and archives tracked coverage. Investment Decision Cards use the same economic meanings even where the transport verb is `research_further` (queue more research) or legacy `promote` (record the decision while retaining evaluation coverage).
**Reversibility.** Queue is **undoable** by returning the candidate to New or Dismissed. Watch is **one-way** through this action; removal requires tracked-company governance. Build is **one-way** for that run, although later builds supersede artifacts. A reasoned Pass is **append-only** and gradeable; its queue dismissal is **undoable** by reopening, but the recorded pass remains in decision history.
**Feedback owed.** Distinguish intent from completion: `Queued — ready for a build`, `Watching — added to watchlist`, `Building — job started` then `Built — evaluation artifacts ready`, or `Passed — avoid decision recorded`. Never show `Built` when only queue state changed.
**Surfaces.** Research → Discovery, Investment Decision Cards, `/discovery` chat actions, and the bounded discovery-build job surface.

### Attest

**Mutates.** `attest` records the owner's explicit claim that a specific position-review memo changed the call; it is the sole owner input to the corresponding Coach P&L target and is idempotent for the same memo.
**Reversibility.** An attestation is **append-only** evidence. It is never inferred from silence, page dwell time, or a repeated click, and there is no generic unattest action.
**Feedback owed.** Show `Attesting…` then either `Attested — review changed the call` or `Already attested — no new count`; failures release the button without incrementing any visible score.
**Surfaces.** Coach position-review memos and the Coach P&L/accountability view.

### Capture

**Mutates.** `capture` appends the owner's raw words and source metadata as an analyst note; downstream classification, draft extraction, or research staging is derived work and must not rewrite the raw capture.
**Reversibility.** Capture is **append-only** provenance. Corrections create a superseding attributable note/draft action; archive may hide the note from active views but does not erase the capture.
**Feedback owed.** Show `Capturing…` then `Captured — added to On My Mind` (and, if applicable, a separate honest derivation status such as `Decision draft queued`). On derivation failure, the receipt must distinguish `Captured` from the failed downstream step.
**Surfaces.** Web/mobile capture boxes, Telegram capture, and any import path that uses the canonical capture-ingest core.

### Grade

**Mutates.** `grade` records an owner-observed outcome or post-exit assessment against an existing decision/position, including exit reason, lessons, and outcome versus thesis. Deterministic scheduled graders are evidence producers, not owner-facing Grade actions, and must remain visibly attributable as system grades.
**Reversibility.** Owner grading is **undoable/correctable** by an explicit re-grade that preserves who changed the assessment and when; it must never rewrite the original decision thesis or conditions. Automated grades are **append-only** observations or superseding runs.
**Feedback owed.** Show `Grading…` then `Graded — outcome recorded`; a re-grade says `Re-graded — assessment updated`. Failure releases the form with the owner's entered text intact.
**Surfaces.** Holding → Position lifecycle, the decision journal/calibration ledger, and scorecards that link each grade back to its source decision.

## Pre-Earnings Brief

**Definition.** A persisted, ticker-scoped preparation artifact for one expected earnings event. It states what the quarter must show, the numbers to check, what to listen for, and the thesis pressure points. Portfolio names generate automatically inside the configured pre-earnings window; evaluation names do so only when `auto_pre_earnings_brief` is explicitly enabled.
**Lives in.** `llm_artifacts` with `purpose='pre_earnings_brief'`; `src/earnings_brief.py`; morning pipeline stage 1c; the Earnings Prep peek.
**Not to be confused with.** The deterministic Earnings Prep template, which is a zero-token fallback, or a generic Ask response, which is conversational and is not the canonical persisted deliverable.

## Post-Earnings Readout

**Definition.** A persisted, ticker-scoped investor artifact for one selected reported fiscal quarter. It separates reported results, management explanation, thesis inference, and falsifiable next-quarter checks. Portfolio names generate automatically after the morning trigger pass; evaluation names generate only from an explicit owner request.
**Lives in.** `llm_artifacts` with `purpose='post_earnings_readout'` and `fiscal_period=<selected transcript period_end>`; `src/earnings_readout.py`; morning pipeline stage 1d; the Post-ER Readout peek. `llm_artifact_store.quarter_index` is the canonical ticker x quarter reader.
**Not to be confused with.** The deterministic Post-ER template, which assembles recorded facts without an LLM and burns zero tokens, or a generic Ask response, which is not persisted or quarter-indexed.

## Full Research Brief

**Definition.** The persisted, ticker-scoped complete research artifact assembled from the governed report body and immutable artifact manifest. Its compact UI label is **Brief**. Library titles use `[TICKER] [Qn yy] Brief` only when the artifact carries that exact fiscal-period identity.
**Lives in.** `report_artifacts.v1.json` and per-artifact manifests under `output/research/`; `src/report/artifacts.py`; the Brief Library and Full Research Brief reader.
**Not to be confused with.** A Pre-Earnings Brief, which prepares for one expected event; a Post-Earnings Readout, which evaluates one reported quarter; or a conversational Ask response.

## Searchable Single-Select

**Definition.** The program-wide app-owned control for choosing exactly one value from a closed option set. Typing filters the active listbox without exposing a separate search field; committing a result updates the owning typed value, while unmatched text creates no value.
**Lives in.** `src/ui/controls.py` and its registered consumers. A native `<select>` may remain only as the hidden form/value carrier beneath the app-owned trigger and listbox.
**Not to be confused with.** A free-text search input, a multi-select listbox, or a related-facet group. Facet dependency is an owning surface behavior; it is not implicit in every Searchable Single-Select.

## Thought Partner

**Definition.** The program's operating identity — a living system that extracts, explores (Socratically), synthesizes, and learns a user Worldview over time; it treats captures as raw material for thinking, not records to file. Storage is the last step, not the product.
**Lives in.** Cross-cutting identity, realized by the capture → explore → distil → Worldview pipeline (`src/capture/`, the On My Mind feed, `src/synthesis/`, `src/llm/anchors.py`).
**Not to be confused with.** The per-ticker analyst workspace (the HTML report deliverable) — that is the *output* of analysis, not the thinking loop.
**Subsumes.** Informal "assistant" / "chatbot" / "CRUD app" descriptions of the program.

## On My Mind

**Definition.** The reverse-chronological living feed of what the analyst is currently thinking about and reading — each item indexed to themes, holdings, and overall positioning, carrying the action ladder **dismiss · save-for-later · discuss · incorporate-into-research · worldview**. The front-of-funnel where the LLM extracts and explores *before* anything is distilled.
**Lives in.** `src/onmymind/feed.py`, the Telegram and dashboard capture surfaces, and the `analyst_notes` read model for `source='capture'`; its `worldview` action stages a candidate Tenet.
**Not to be confused with.** The Worldview (durable, synthesized) — On My Mind is transient working memory that feeds it.
**Subsumes.** The former **Wondering** concept and its retired `wondering_detect` classifier. The legacy-named `LEDGER_RESEARCH_TAP` flag still gates live `capture_intent` classification and is compatibility debt; On My Mind remains strictly broader — reading and exploration, not just self-posed questions.

## Worldview

**Definition.** The durable, evolving model of how the analyst thinks — the synthesized set of Tenets that subtly conditions investment reasoning (hold / add / trim / sell / evaluate).
**Lives in.** Current and proposed Tenets in `insight_notes`; `src/synthesis/tenets.py` owns the store, `src/pipeline/worldview_panel.py` owns the review surface, and `src/llm/anchors.py` composes the flag-gated reasoning anchor.
**Not to be confused with.** A per-ticker thesis (company-specific, in `micro_thesis/holdings/`) — the Worldview is cross-company, about the analyst's *own* reasoning.
**Subsumes.** The merged `influence` analyst-notes kind (PR #701), which is superseded by Tenets.

## Tenet

**Definition.** A single revisable belief-unit in the Worldview — a principle about *how the analyst invests* — with provenance to the insights that formed it; the system proposes revisions the analyst approves and flags contradictions when a new insight conflicts with a standing Tenet.
**Lives in.** `insight_notes` with `kind='tenet'`, owned by `src/synthesis/tenets.py`; current Tenets compose the Worldview and proposed Tenets remain in the approval queue.
**Not to be confused with.** A **conviction** (see below) — a `conviction` is a *1–5 confidence rating on a position/decision* (`bucket_for_conviction`, conviction calibration/Brier in `src/advisor/`, and the `conviction` field on `decision_capture`). A Tenet is a cross-company belief about *method*, not a confidence level on a name. Also distinct from a `musing` (an in-the-moment captured thought) and an `insight_note` of `kind='theme'` (a topic cluster, not a belief).
**Subsumes.** — (was proposed as "Conviction" 2026-07-01; renamed to avoid collision with the entrenched `conviction` rating.)

## conviction (rating)

**Definition.** A 1–5 confidence score the analyst assigns to a position/stance/decision, used for calibration (hit-rate by conviction bucket, Brier scoring).
**Lives in.** `src/advisor/context.py`, `src/advisor/memos.py`, the sizing-audit conviction column, `src/research/decision_capture.py` (`conviction` field).
**Not to be confused with.** A **Tenet** (a Worldview belief-unit). Lowercase `conviction` = a rating; a Tenet = a belief.

## Source Taxonomy Component

**Definition.** An immutable, exact source-side XBRL concept, axis, or member identity keyed by its taxonomy namespace, local name, taxonomy name, and taxonomy version, with its source metadata and provenance commitments.
**Lives in.** `source_taxonomy_components` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A **Canonical Metric**; a Source Taxonomy Component describes what a filer or taxonomy asserted, not the economic meaning selected for analysis.
**Subsumes.** Source QName, taxonomy concept, source axis, and source member only when the exact source identity is retained.

## Source Observation Taxonomy Assertion

**Definition.** An immutable, hash-committed assertion of the exact taxonomy name and version used by one preserved reported observation. It cites and verifies the exact extraction run, fact-cell identity seal, reported-anchor payload, observation payload, extraction output, raw entry, and sealed observation set, all of which must exist before the assertion's knowledge clock.
**Lives in.** `source_observation_taxonomy_assertions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A Source Taxonomy Component; the assertion proves which taxonomy governed one observation, while the component identifies one concept, axis, or member inside that taxonomy.

## Canonical Metric

**Definition.** A stable named economic identity whose meaning evolves only through explicit Canonical Metric Definition Revisions, used to compare facts across source taxonomies without making a source QName part of its identity.
**Lives in.** `canonical_metrics`, `canonical_metric_definition_revisions`, `canonical_metric_cells`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A **Source Taxonomy Component** or a fact value; a Canonical Metric defines intended economic meaning, while source facts remain independent assertions.
**Subsumes.** Normalized economic metric and analytical metric.

## Canonical Axis

**Definition.** A stable source-independent dimension axis identity that may be used in Canonical Metric Cells only after explicit source-axis admission.
**Lives in.** `canonical_axes`, `canonical_metric_cell_dimensions`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A source XBRL axis QName, which remains an exact Source Taxonomy Component.
**Subsumes.** Normalized dimension axis and analytical dimension axis.

## Canonical Member

**Definition.** A stable source-independent member identity owned by one Canonical Axis and admitted from exact source members through revisioned mapping evidence.
**Lives in.** `canonical_members`, `canonical_metric_cell_dimensions`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A source XBRL member QName or typed-member value; those remain source evidence and fail closed until admitted.
**Subsumes.** Normalized dimension member and analytical dimension member.

## Source Dimension Mapping Revision

**Definition.** An append-only bitemporal decision mapping one exact source axis or member component to a Canonical Axis or Canonical Member under explicit policy, evidence, and reviewer authority.
**Lives in.** `source_dimension_mapping_revisions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A Metric Mapping Revision, which maps a source concept to a Canonical Metric.
**Subsumes.** Source-axis mapping and source-member mapping.

## Metric Mapping Revision

**Definition.** An append-only, bitemporal decision that records whether one exact Source Taxonomy Component maps to a Canonical Metric and under which policy, method, constraints, evidence, and reviewer authority.
**Lives in.** `metric_mapping_revisions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A Canonical Metric Definition Revision; this is source-to-metric admission evidence, not a change to a metric's meaning.
**Subsumes.** Concept mapping, equivalence decision, and mapping policy result.

## Canonical Metric Cell

**Definition.** The source-independent coordinate for a Canonical Metric at one reporting entity, period, canonical dimension set, unit family, accounting basis, consolidation scope, and optional security scope.
**Lives in.** `canonical_metric_cells` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A v2 Fact Cell; a Canonical Metric Cell intentionally excludes source QName and taxonomy version so multiple retained source assertions can bind to it.
**Subsumes.** Canonical fact coordinate and normalized metric cell.

## Fact-Cell Canonical Binding Revision

**Definition.** An append-only bitemporal interpretation of one preserved source observation within a v2 Fact Cell. A reported observation may bind through its exact taxonomy assertion, Metric Mapping Revision, and Source Taxonomy Component to one Canonical Metric Cell; a derived observation without an explicit canonical basis is instead terminally quarantined with a committed reason.
**Lives in.** `fact_cell_canonical_binding_revisions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A one-binding-per-cell shortcut, source fact mutation, or deduplication; each observation owns independent revision history and bindings never merge or erase source assertions.
**Subsumes.** Fact-to-metric binding and canonicalization link.

## Ontology Snapshot Seal

**Definition.** A hash-committed membership set of the ontology records known by an explicit cutoff, used to admit later research snapshots reproducibly.
**Lives in.** `ontology_snapshot_seals`, `ontology_snapshot_members`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A wall-clock database backup; it is an explicit bitemporal governance cutoff with verified members.
**Subsumes.** Ontology seal and mapping snapshot.

## Canonical Fact Candidate Universe

**Definition.** The exhaustive, ordered, hash-committed set of admitted source observations bound to one Canonical Metric Cell as known at an explicit cutoff. Its membership is derived only from sealed admission and ontology records; callers cannot supply, omit, or reorder candidates.
**Lives in.** `canonical_fact_candidate_universe_revisions`, `canonical_fact_candidate_dispositions`, and `src/provenance/canonical_fact_resolution.py`.
**Not to be confused with.** A source-fact candidate set; this set spans every source QName bound to the one canonical coordinate.
**Bound.** Relation construction is intentionally capped at 500 admitted observations per Canonical Metric Cell and cutoff; exceeding it fails closed and requires an explicit partitioning policy rather than an unbounded pairwise graph.

## Canonical Fact Relation Set

**Definition.** An immutable, revisioned assertion graph over a Canonical Fact Candidate Universe. It preserves duplicate-entry ordinals and records equivalence, conflict, amendment, recast, and supersession evidence without collapsing source assertions.
**Lives in.** `canonical_fact_relation_set_revisions`, `canonical_fact_relation_assertions`, and `src/provenance/canonical_fact_resolution.py`.
**Not to be confused with.** A value-selection rule; relations are evidence, while a Canonical Fact Resolution applies a named deterministic policy to that evidence.

## Canonical Fact Resolution

**Definition.** An append-only, bitemporal selection or explicit unresolved/retired outcome for one Canonical Metric Cell. A resolution references the exact sealed candidate universe and relation set used, and never averages conflicting source assertions.
**Lives in.** `canonical_fact_resolution_revisions`, `canonical_fact_resolution_snapshot_seals`, and `src/provenance/canonical_fact_resolution.py`.
**Not to be confused with.** A source-cell resolution; it is the cross-QName decision at a canonical coordinate.

## Document Processing Obligation

**Definition.** A revisioned, bitemporal requirement that one exact recorded document complete one named processing lane under a committed policy, derived exhaustively from source obligation, document, and immutable evidence state as known at an explicit cutoff.
**Lives in.** `document_processing_obligation_revisions`, `src/provenance/research_snapshot.py`, and `alembic/versions/0245_document_processing_research_snapshots.py`.
**Not to be confused with.** A source acquisition duty; a Document Processing Obligation begins from a recorded document and governs the transformations required before research use.
**Subsumes.** Required parser lane, extraction requirement, and document-processing duty.

## Document Processing Disposition

**Definition.** The final sealed outcome for one Document Processing Obligation, with exactly one terminal status and an exhaustive ordered set of committed evidence members that prove success or a valid terminal exception.
**Lives in.** `document_processing_disposition_headers`, `document_processing_disposition_members`, `document_processing_disposition_seals`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** A mutable job status or retry log; it is an immutable final decision whose members and seal are verified symmetrically against live evidence.
**Subsumes.** Processing result, lane disposition, and extraction outcome.
**Current admission boundary.** `filing_xbrl` can succeed only through the exact native 0242 extraction-disposition seal and its verified Source Fact Publication. Every other applicable lane fails closed until a native closure adapter can recompute its ordered outputs and final seal; synthetic or caller-attested commitments never admit.

## Document Processing Snapshot

**Definition.** An exhaustive, ordered, hash-sealed set containing every applicable Document Processing Obligation and its one verified Document Processing Disposition as known at one explicit cutoff.
**Lives in.** `document_processing_snapshot_headers`, `document_processing_snapshot_members`, `document_processing_snapshot_seals`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** A current processing dashboard or a caller-supplied document list; membership is derived internally and cannot change when later evidence arrives.
**Subsumes.** Processing completeness seal and document readiness snapshot.

## Native Processing Evidence Seal

**Definition.** An immutable header/member/final-seal publication derived from one exact successful extraction run and the complete ordered native rows required for a supported Document Processing lane. Its public verifier re-derives membership from the pinned native run and compares every locator, content commitment, clock, and final member-set digest.
**Lives in.** `document_processing_evidence_headers`, `document_processing_evidence_members`, `document_processing_evidence_seals`, `src/provenance/document_processing_evidence.py`, and `alembic/versions/0248_native_processing_closure_adapters.py`.
**Not to be confused with.** A successful extraction attempt or caller-supplied evidence list; neither proves that the native output set is complete.
**Current admission boundary.** HTML hierarchy, PDF native text/OCR/tables, standalone image OCR, PPTX slides/charts/tables, XLSX workbook/sheets/named tables, transcript turns, and fully time-coded transcript speakers can seal. Each applicable lane still fails closed when its exact native inventory is missing, quarantined, old, or tampered.

## PDF Table Extraction Artifact

**Definition.** An append-only header/member/final-seal publication over one exact PDF byte stream and one pinned PyMuPDF/MuPDF dual-detector configuration. Its ordered members preserve every page disposition and detected table, row, cell, coordinate, nested commitment, and explicit no-table proof.
**Lives in.** `pdf_table_extraction_artifact_headers`, `pdf_table_extraction_artifact_members`, `pdf_table_extraction_artifact_seals`, `src/provenance/pdf_table_extraction.py`, and `src/provenance/document_processing_evidence.py`.
**Not to be confused with.** A claim of semantic table exhaustiveness. The artifact proves exact detector-relative coverage; encrypted, scanned, image-only, malformed, ambiguous, or resource-capped inputs remain quarantined and cannot satisfy the `pdf_table` lane.

## Research Snapshot

**Definition.** An immutable, ordered, hash-sealed research evidence boundary that binds exact Document Processing Snapshots and every requested verified corpus, search, fact-publication, ontology, canonical-resolution, canonical-fact-projection, and embedding-promotion seal at one cutoff.
**Lives in.** `research_snapshot_headers`, `research_snapshot_members`, `research_snapshot_seals`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** A database backup, current-view query, or generated research report; it is the reproducible admission boundary those consumers must cite.
**Subsumes.** Research evidence snapshot and research-readiness seal.

## Research Snapshot Admission

**Definition.** A typed, fail-closed verification result proving that a Research Snapshot contains exactly one valid terminal member for every requested lane and that every referenced commitment and clock still matches immutable live evidence.
**Lives in.** `ResearchSnapshotAdmission`, `admit`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** Snapshot creation or optimistic readiness; admission is granted only after symmetric verification of sealed membership and public-verifier results.
**Subsumes.** Research readiness decision and snapshot admission result.

## Source Fact Publication Stream

**Definition.** Database-assigned, monotonically ordered events over verified sealed Source Fact Publications. Stream order is strict but gaps are allowed; consumers use it as the replay high-watermark.
**Lives in.** `source_fact_publication_stream`, `source_fact_stream_clock`, `src/provenance/source_fact_stream.py`, and `alembic/versions/0246_source_fact_publication_stream.py`.
**Not to be confused with.** Knowledge or recorded clocks, or a publication member ordinal; the stream orders sealed publication events for consumption and replay.

## Canonical Fact Projection Generation

**Definition.** An immutable checkpoint or delta read generation derived from one exact Canonical Fact Resolution Snapshot and Ontology Snapshot at a verified Source Fact Publication Stream watermark. It commits bounded batches, deterministic digest buckets, explicit upserts and tombstones, and the effective fact count.
**Lives in.** `canonical_fact_projection_generations`, `canonical_fact_projection_entries`, `canonical_fact_projection_batches`, `canonical_fact_projection_buckets`, `canonical_fact_projection_seals`, and `src/search/canonical_fact_projection.py`.
**Not to be confused with.** The legacy whole-plane Structured Fact Search Projection; a Canonical Fact Projection contains only selected canonical metric coordinates and cannot be admitted without its strict audit receipt.

## Heterogeneous Retrieval Trace

**Definition.** An immutable, replay-verifiable trace over one exact Research Snapshot that records the closed candidate universe, deterministic ranker inputs, ordered `FactHit | DocumentHit` results, and every returned hit's source commitment.
**Lives in.** `heterogeneous_retrieval_trace_headers`, `heterogeneous_retrieval_trace_candidates`, `heterogeneous_retrieval_trace_results`, `heterogeneous_retrieval_trace_seals`, and `src/search/heterogeneous_retrieval.py`.
**Not to be confused with.** An answer citation list or an unsealed vector-search response; the trace proves what retrieval considered and returned before synthesis.

## Embedding Runtime Artifact

**Definition.** A canonical, path-free, content-addressed manifest of every local file, package/runtime version, execution provider, and explicit setting that can affect one embedding model coordinate. Its digest is shared by evaluation, promotion, successful vector artifacts, vector projection seals, and exact semantic receipts; the local bytes and component versions are verified before and after offline model initialization.
**Lives in.** `src/search/embedding_runtime_artifact.py`, `search_embedding_model_promotions.runtime_artifact_json`, `search_embedding_model_promotions.runtime_artifact_sha256`, `search_embedding_artifacts.runtime_artifact_sha256`, `search_projection_seals.runtime_artifact_sha256`, and `alembic/versions/0249_embedding_runtime_artifact_binding.py`.
**Not to be confused with.** A model name, cache path, download URL, or digest inferred after a build. Historical unbound rows remain historical and cannot be promoted into investor-grade exact semantic retrieval.

## Exact Semantic Retrieval Receipt

**Definition.** A locally recomputable commitment to the complete ordered top-k from a bounded full scan of one sealed canonical float32 vector projection. It binds the query and query vector, current embedding promotion and evaluation, exact Embedding Runtime Artifact, model and dimensions, projection/config/storage seals, artifact set, scan cap, scores, and ordering.
**Lives in.** `src/search/exact_semantic.py` and the semantic-receipt section of `src/search/heterogeneous_retrieval.py`.
**Not to be confused with.** An opaque vector-service response or approximate-nearest-neighbor receipt. Those cannot enter an investor-grade Heterogeneous Retrieval Trace unless a separately governed evaluation and admission contract is added.

## Legacy Canonical Parity Report

**Definition.** A cutoff-pinned, read-only, keyset-paged comparison that follows the exact accepted legacy-evidence bridge into one ontology-bound Canonical Metric Cell and its sealed, audited Canonical Fact Projection entry. It records one explicit terminal disposition for every legacy row, exact field differences, duplicate-coordinate cardinality, truncation, and cutover readiness.
**Lives in.** `src/provenance/legacy_canonical_parity.py`.
**Not to be confused with.** Row-count equality, label/QName matching, or a live reader cutover. `cutover_ready` requires a complete untruncated scan with no mismatches or blocking legacy-side dispositions; canonical-native-only coordinates are reported separately.

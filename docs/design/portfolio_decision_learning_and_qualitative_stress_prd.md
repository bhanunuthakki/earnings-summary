# Portfolio Decision Learning and Qualitative Stress — Product Requirements Document

**Status:** Proposed for owner approval on 2026-08-14; implementation not started.  
**Independent review:** PASS on 2026-08-14 from product-effectiveness and
provenance/operational-risk judges after two HOLD-and-revision cycles.  
**Audience:** Repository owner and implementation agents.  
**Umbrella PRD:** `docs/design/personal_investment_partner_prd.md`.  
**Scope:** Trustworthy owner-decision capture, evidence-level thesis-evaluation deduplication,
lightweight qualitative common-drawdown analysis, and the WIX learning-record repair.  
**Document authority:** This is a design artifact, not a Layer-1 directive. It does not
authorize a live migration, scheduled-task change, holdings mutation, or brokerage action.

---

## 1. Executive summary

The product should improve the owner's investment judgment without creating a false-precision
risk laboratory or a noisy archive of repeated machine conclusions.

This program makes four changes:

1. Treat one distinct thesis-and-evidence state as one owner-facing evaluation episode. A
   scheduler may execute repeatedly, but unchanged evidence must not create another evaluation
   record or another prompt.
2. Make every confirmed add, trim, hold, sell, or target allocation preserve what the owner
   believed, why the owner acted, the portfolio context, and the alternative use of capital.
3. Add a compact, research-backed qualitative playbook for common drawdowns using existing
   business factors, holdings weights, and named scenarios. It does not require a decades-long
   local price archive.
4. Repair the WIX learning path without fabricating historical beliefs: preserve the entry
   standpoint, later thesis development, the current capital-and-attention exit, and a future
   post-exit comparison with AVDV.

The platform remains pull-only. Recording a decision never places, stages, or assumes execution
of a trade.

## 2. Owner decisions already recorded

The following owner decisions were recorded on 2026-08-14 through the canonical session-decision
capture path. These are inputs to this PRD, not implementation claims:

- **WIX decision 135:** full sell near $85; 2.5444% size from the 2026-08-13 materialized
  holdings snapshot; low conviction; tax-deferred IRA; company and much of the thesis remain
  intact; capital-and-attention reallocation rather than a fabricated thesis break; full
  proceeds designated for AVDV.
- **AVDV decision 136:** add 2.5444% using the WIX proceeds; tax-deferred IRA; target a
  4.5%-5.0% portfolio weight; intended as international small-cap value diversification, not a
  short-term hedge.
- **AVDV sizing intent 7:** midpoint target of 4.75%, explicitly marked for verification after
  holdings refresh.

The current materialized holdings snapshot does not evidence an existing AVDV position. It can
therefore verify the 2.5444% WIX funding amount but cannot verify that the proposed AVDV purchase
will reach 4.5%-5.0%. The product must show this as an unresolved basis mismatch, not silently
convert absence into a confirmed zero weight and not overwrite the owner's target.

## 3. Current-state findings

### 3.1 Thesis evaluation and prompting

- WIX has 34 stored `warn` evaluations but only two distinct normalized evidence payloads: one
  repeated eight times and one repeated 26 times.
- `persist_verdict` intentionally appends on every execution and stores no semantic evidence
  fingerprint. The evaluator's invocation fingerprint also includes prior evaluation rows, so
  repeated runs continue to produce more history.
- Execution history belongs in `pipeline_runs`, `pipeline_attempts`, and stage-transition
  telemetry. It is not owner learning.
- The alert layer already deduplicates active signatures, so 34 evaluations did not produce 34
  simultaneous cards. WIX currently has three historical alerts and one pending KPI alert.
- There is no evidence-episode acknowledgement seam. The owner cannot say “I have seen and
  accepted this warning” and reliably suppress it until something material changes.

### 3.2 Decision and thesis learning

- The canonical WIX thesis file is substantively useful and has `last_updated=2026-06-01`.
  Because WIX has no thesis-ledger entry or explicit owner-attribution receipt for that edit, the
  date must not be described as a proven owner-accepted thesis update.
- WIX has no `thesis_ledger_entries`. Git contains thesis-file revisions, but mechanical edits and
  owner belief changes are not separated in an owner-facing timeline.
- Before the decisions above, WIX had one owner initiation and three advisor decisions. Unadopted
  advisor recommendations must not be counted as owner judgment.
- Historical action/outcome labels can call a held owner decision “ignored” or “wrong” based on
  price movement. These are not reliable process-quality conclusions.
- The canonical owner-decision CLI writes the right decision fields but is not idempotent by
  session reference. An interrupted retry can duplicate an owner decision.

### 3.3 Portfolio risk and diversification

- Interest-rate risk is present in the `rates/duration` business-factor taxonomy, a 10-year-rate
  sensitivity, and rate-cut/rate-hike scenarios.
- The current numeric 10-year-rate beta is not decision-grade: the estimator applies log returns
  to yield levels, while scenario shocks are absolute basis-point changes. Existing beta rows must
  remain quarantined from owner conclusions until a unit-correct, versioned replacement exists.
- `business_factor_exposures` exists but currently contains no rows after a failed schema-drifted
  refresh. Empty results are not consistently distinguished from true zero exposure.
- Existing named scenarios already cover useful common shocks: rates/inflation, recession, oil,
  currency, sovereign/funding stress, SaaS multiple compression, advertising recession, LatAm
  joint stress, and GLP-1 pricing pressure.
- Current account-linked drawdown is not admissible as primary decision evidence. Historical
  security data can produce a static-current-weight counterfactual, but the common history is
  stale and is unnecessary for the first qualitative layer.

### 3.4 Database readiness judgment

Physical integrity passes, and an aligned runtime successfully wrote and read the two owner
decisions and the AVDV sizing intent; the decision-journal view exposes both decisions. That proves
the existing database can persist these ordinary objects through an aligned writer.

It does not prove that the current scratch checkout can safely migrate the live database. At this
review, the live database reports `0012_close_operation_event_detail_reason`, while the scratch
checkout's migration head is `0012_add_readme_update_budgets` and its Git topology is divergent.
No migration or write-path change may run from that checkout until histories are reconciled in an
aligned worktree and drift inspection reports no mismatch.

Even after alignment, the portfolio-aware review remains degraded: business-factor
personalization is unavailable, repeated evaluations overstate history, the rates beta is
dimensionally invalid, and decisions 135/136 do not yet contain the typed point-in-time checkpoint
defined below.

## 4. Goals and non-goals

### 4.1 Goals

1. One material evidence state produces one owner-facing thesis-evaluation episode.
2. Acknowledged warnings stay quiet until evidence, rules, or severity materially changes.
3. Every consequential owner decision is idempotent, point-in-time, and reconstructable.
4. Every add/trim/hold/sell review includes a holdings-native qualitative diversification and
   common-drawdown lens with explicit availability.
5. The WIX exit can later be evaluated against the belief held at exit and the AVDV alternative,
   without confusing outcome luck with process quality.

### 4.2 Non-goals

- Brokerage execution, order staging, or assumed fills.
- A decades-long local price warehouse or shock-specific correlation estimator.
- A precise expected-loss estimate derived from qualitative regimes.
- Account linkage as the primary holdings or drawdown source.
- A second hard-coded holdings-factor map.
- Turning unchanged evaluator executions into owner history.
- Turning unadopted advisor recommendations into owner decisions.
- Backdating reconstructed WIX beliefs or manufacturing a thesis break.

## 5. Priority P0 — Trustworthy evaluation and decision units

### 5.1 P0-A: Semantic thesis-evaluation episodes

Preserve `thesis_evaluations` as immutable legacy/raw evaluation records. Add a separate
`thesis_evaluation_episodes` owner-facing store and a
`thesis_evaluation_episode_members` mapping from each episode to every retained legacy/raw row.
New duplicate executions are represented by pipeline run telemetry and an episode check receipt;
they do not append another raw evaluation row.

Define `semantic_input_sha256` as a deterministic SHA-256 over:

- ticker and canonical thesis-content hash;
- normalized hard- and soft-rule definitions plus ruleset version;
- normalized accepted observations used by the verdict: metric identity, period end, value, unit,
  currency where relevant, accepted value, and material source/restatement semantics;
- evaluator semantic version.

Exclude `run_id`, execution timestamp, prior evaluation rows, UI state, serialization order,
mechanical thesis metadata, and provenance-row IDs whose repointing does not change accepted
evidence. Store `ruleset_sha256` separately for inspection. Define the episode key as ticker,
fingerprint-policy version, semantic input hash, and resulting severity. A severity change can
therefore never be suppressed by acknowledgement of the prior episode. In a deterministic
evaluator it should occur only when semantic inputs, rules, or evaluator version changed; if it
does not, persistence fails loudly as nondeterminism.

Enforce one row per episode key. A repeated execution updates only `last_checked_at`,
`last_seen_at`, and `duplicate_run_count`; it does not append a new episode or change
`evidence_as_of`. Every scheduler execution remains auditable in the existing pipeline run and
attempt stores. A compatibility view exposes distinct episodes to history, streak, dashboard,
report, command-center, and position-lifecycle consumers while the raw legacy table remains
queryable for audit.

Create a new episode when any of the following occurs:

- a new source period or a restatement changes evidence;
- the canonical thesis or break rules materially change;
- the evaluator semantic version changes;
- the resulting severity changes.

Legacy rows cannot support the forward fingerprint because they lack historical thesis hashes and
some accepted-source/restatement fields. Backfill them with a separate `legacy_v0` policy based
only on normalized stored rule/soft-rule payloads after volatile timestamps are removed. Mark
provenance completeness `partial`, never compare a `legacy_v0` hash with the forward policy, and
map every source row through the membership table. Retain earliest episode time, latest seen time,
occurrence count, and all 34 WIX legacy IDs. The expected owner-facing WIX result is two partial
legacy episodes, not 34 learning events.

### 5.2 P0-B: Evidence-specific acknowledgement and notification suppression

Acknowledgement belongs to the episode ID, not the ticker or semantic hash globally. Episodes use
the lifecycle `unreviewed -> acknowledged | acted_on | superseded`. The owner can acknowledge with
timestamp, optional note, and optional next-review date. Acting on an episode links one idempotent
decision and, only for a real thesis change, one ledger entry. All surfaces use one shared action
core.

After acknowledgement, do not prompt again for the same episode. Re-notify only when:

- a new episode exists;
- a newly computed episode has worse severity;
- a restatement changes a material observation; or
- the owner explicitly sets a review date and a new unconsumed review cycle becomes due.

Define one centralized `is_episode_actionable(episode_id, now)` predicate. Every producer and
reader—including trigger creation, coach pings, Senior Partner Brief, Inbox, thesis-history/streak
views, ticker command center, report sections, and position review—uses it or the compatibility
read model. Reuse existing alert-signature dedup for delivery and add an idempotent delivery receipt
keyed by episode ID, channel/surface, and review-cycle ID. Consuming a review cycle stamps
`review_notified_at`, so a due date cannot fire on every later run. Existing coach-ping
acknowledgement is not episode acknowledgement; it must call the shared episode action core when
linked. Alert state is presentation state; the episode remains analytical state. Daily runs may
report `deduplicated_no_change` in telemetry, but must not create a card, coach ping, or “new
warning” count.

### 5.3 P0-C: Idempotent owner decision checkpoint

Before confirmation, create a typed decision checkpoint with a durable `source_event_id`
(capture/turn/owner-confirmation identity) containing:

- action, proposed delta, target band, price level, account, instrument, and horizon;
- an embedded immutable holdings-basis payload or content hash, source and as-of timestamp,
  relevant weights, and availability per ticker (`observed`, `missing_from_snapshot`, or
  `source_unavailable`), plus any target/delta mismatch;
- thesis state (`intact`, `watch`, `broken`, or `not_the_reason`), canonical thesis hash and
  excerpt, and what changed since the prior owner decision;
- why now, conviction, falsifier, portfolio role, qualitative stress implication, and alternative
  use of capital;
- prior owner-decision ID and any explicitly adopted advice ID;
- schema version and canonical payload SHA-256.

Enforce uniqueness on owner, source channel, source-event ID, and checkpoint schema version—not on
session plus ticker/action, because one session may contain two legitimate same-action decisions.
The same key plus the same payload hash returns the existing result. The same key plus a different
payload hash raises a conflict requiring an explicit correction/amendment event.

Confirmation is one database transaction that writes or links the checkpoint, exactly one owner
decision, an idempotently keyed target-band/sizing-intent row when present, and an optional thesis
ledger entry. Failure rolls back all of them. A capital-allocation or attention decision with no
thesis change writes no thesis-ledger entry. A real current belief change writes one accepted
thesis update; reconstruction never backdates it.

For target allocations, preserve both the trade delta and the target band. If the holdings basis
cannot reconcile them, the decision remains valid but displays `target_unverified` until a later
holdings refresh confirms or contradicts it.

After code/schema alignment, create one idempotent, current-dated paired repair envelope for WIX
decision 135, AVDV decision 136, and sizing intent 7 without altering their original `made_at` or
claiming the envelope was contemporaneous. Freeze the 2026-08-13 holdings snapshot identity,
WIX's observed 2.5444% weight, AVDV's `missing_from_snapshot` state, both proposed deltas, the
4.5%-5.0% target, current WIX thesis hash/excerpt, prior WIX owner decision 53, mutual
source/alternative linkage, decision horizon, and retrospective provenance. Any unstated AVDV
conviction or falsifier remains explicitly `not_provided`; it is never invented.

## 6. Priority P1 — Qualitative common-drawdown and investor-learning layer

### 6.1 P1-A: Versioned regime playbook

Add a pure, typed, version-controlled `RegimeDefinition` registry keyed to the existing business
factor taxonomy and named scenarios. Initial regimes:

- demand-led recession;
- inflation and long-duration/rates shock;
- sovereign-debt or funding crisis;
- currency crisis;
- oil-supply shock;
- SaaS/prosumer-software multiple compression;
- advertising recession;
- LatAm credit stress; and
- GLP-1 pricing pressure where relevant.

Each definition contains factor IDs, named-scenario IDs, direction, one-sentence transmission
mechanism, likely co-movement groups, offsets or relative beneficiaries, competing effects,
historical analog references, evidence citations, last-reviewed date, registry version, and
confidence policy.

Outputs are qualitative only: `benefits`, `resilient`, `mixed`, `vulnerable`, or
`highly_vulnerable`. They also state personalization availability, coverage, as-of, provenance,
confidence, and why the assets may move together in that shock.

Classification uses a versioned deterministic policy. Each regime-to-factor mapping carries an
effect ordinal from `-2` (highly vulnerable) through `0` (mixed) to `+2` (benefits) and a confidence
multiplier fixed in the registry. Existing unsigned 0-1 factor loadings scale the effect; they do
not determine its direction. For a holding, sum
`loading * (effect_ordinal / 2) * confidence` and divide by
`max(1, total applicable loading)`, capped to `[-1, 1]`. The floor of one preserves absolute
exposure magnitude—a 0.10 incidental loading cannot score like a 1.0 loading—while the denominator
prevents many mapped factors from mechanically exceeding the scale. Beneficiary/offset factors
contribute positive effects, vulnerabilities contribute negative effects, and documented
competing effects are included as separately mapped factors rather than free-text overrides.

For the book, weight holding scores by materialized portfolio weight and normalize only across
covered non-cash weight. Coverage is covered non-cash portfolio weight divided by total non-cash
portfolio weight. Report the denominator, excluded tickers, factor as-of, and registry version.
Coverage below 70% caps availability at `partial`; missing/stale factor state never becomes a zero
score. Convert the internal score deterministically:

- score at or above `+0.50`: `benefits`;
- `+0.15` to below `+0.50`: `resilient`;
- above `-0.15` to below `+0.15`: `mixed`;
- above `-0.50` to `-0.15`: `vulnerable`;
- `-0.50` or below: `highly_vulnerable`.

The score is an implementation/ranking aid and is not presented as expected loss. For a proposed
action of at least 0.50% of portfolio weight, recompute before/after weights. Classify the regime
concentration as increased or decreased only if the score moves by at least 0.02 or crosses a
category boundary; otherwise return `no material change`. Thresholds and effect mappings change
only through a registry-version bump.

Fallback order:

1. Populated business-factor loadings combined with materialized holdings weights.
2. Existing named-ticker scenario membership, labeled partial coverage.
3. Generic regime commentary labeled `portfolio personalization unavailable`.

Before fallback, use an instrument-aware proposed-security adapter so an add can be evaluated even
when the security is not yet in materialized holdings. A proposed operating company may use an
existing valid factor row or named scenario; the adapter never runs an operating-company extractor
implicitly. A proposed ETF uses existing ETF profile, holdings, country, style, sector, and
constituent data. It aggregates only evidenced dimensions and, for business factors, only
constituents with compatible factor rows; every dimension carries its own look-through coverage and
excluded weight. It never hardcodes AVDV or invents issuer-level factors for a fund.

For the paired WIX/AVDV checkpoint, construct one synthetic after-state from the same immutable
holdings basis: remove WIX's observed 2.5444% and add the owner-directed 2.5444% AVDV delta, while
keeping the 4.5%-5.0% target separately `target_unverified`. Existing AVDV ETF holdings/profile data
may support country/style/sector and partial constituent-factor analysis; unsupported or stale
dimensions stay partial/unavailable. Generic commentary alone cannot claim a holdings-native
diversification improvement.

Freshness follows a versioned source-cadence policy. Initial maximum ages are three calendar days
for materialized holdings, 90 days for company business-factor rows, 120 days for ETF holdings or
profile composition, and 365 days for regime-registry research review. A business-factor row is
stale immediately if its `input_sha` is incompatible with the current controlling evidence pack,
even within 90 days. Each source exposes its observed/as-of time and cutoff; effective `as_of` is
the oldest controlling input. Crossing a cutoff downgrades availability at least one level, and a
required stale input with no valid fallback makes that dimension unavailable. Policy version and
exact-boundary tests govern any future cadence change.

Every add/trim/hold/sell review surfaces the two most relevant regimes. Selection is deterministic:
named-scenario membership, then absolute material before/after factor-score delta, then baseline
vulnerability, then registry priority. Named-scenario fallback uses the same effect ordinal and
action-materiality policy but is labeled partial. Generic commentary cannot claim a portfolio
concentration change. Without an explicit action delta, return `no measurable change` rather than
infer one. Golden fixtures freeze populated, partial, stale, unavailable, offset-heavy, and
named-scenario-fallback behavior, including different classifications for otherwise identical 0.10
and 1.0 loadings and one paired WIX-sell/AVDV-add after-state.

### 6.2 P1-B: Factor availability and holdings-native provenance

Repair and rerun the existing business-factor refresh only after code and schema are aligned and a
dry run proves coverage. Empty, missing, stale, partial, and true-zero states must be distinct.
The qualitative lens uses materialized holdings as the primary portfolio source and never silently
substitutes account-linked history.

Position review, the Risk page, and bounded existing coaching share the same typed output. Do not
create another scheduler or factor store.

### 6.3 P1-C: WIX learning timeline and owner/advisor separation

Create one current-dated, visibly non-contemporaneous reconstruction linked to the source
conversation and current WIX thesis:

1. Entry: low-conviction suspected value trap, small retirement position, Base44 optionality,
   legacy-core and margin-drag concerns.
2. Development: Base44 validation strengthened optionality; underwriting moved toward a two-engine
   whole-company rerating while retaining opacity, core-growth, buyback/leverage, and margin risks.
3. Exit: full sale near $85; thesis not clearly broken; low conviction and monitoring burden;
   prosumer demand and SaaS-duration exposure are contributors; capital reallocated to AVDV.

Owner learning and calibration include owner decisions plus advisor advice the owner affirmatively
adopted. Unadopted advisor recommendations remain advisor-performance evidence only.

Action reconciliation evaluates intended position delta and later holdings/fills. Price outcome is
reported separately from process quality. When WIX is actually absent from refreshed holdings,
close its lifecycle using the confirmed exit decision and schedule an owner-reviewed postmortem.

## 7. Priority P2 — Secondary repairs and learning closure

### 7.1 P2-A: Unit-correct rate sensitivity

Keep legacy `rate_beta_10y` rows quarantined from ranking and owner prose. Replace the yield
transformation with declared first differences in percentage points or basis points, express
scenario shocks in the same unit, version the new metric, backfill it, and pass unit-regression
tests before removing the quarantine. The qualitative `rates/duration` lens remains the primary
MVP signal.

### 7.2 P2-B: Exit postmortem and alternative-capital comparison

After holdings confirm the WIX exit:

1. Freeze the decision and thesis version that existed immediately before exit.
2. Draft `exit_reason`, `lessons`, and `outcome_vs_thesis` from owner decisions first.
3. Require an owner glance or correction.
4. Use the horizon frozen in the paired checkpoint. Compare WIX with AVDV as a realized
   reallocation only if refreshed holdings or fill evidence confirms AVDV timing and size. If WIX
   closes but AVDV execution is not evidenced, label the comparison `counterfactual_not_executed`;
   do not attribute AVDV timing or sizing to the owner.
5. At that horizon, separate selection, sizing, timing, and subsequent price luck.
6. Feed a lesson into coaching only when action, horizon, and evidence are coherent.

## 8. UX requirements

- The Ledger shows `owner`, `adopted advisor`, and `unadopted advisor` distinctly.
- A decision checkpoint shows current weight, proposed delta, target band, data as-of, and any
  mismatch before confirmation.
- Thesis health displays episode count and latest material evidence date, not scheduler run count.
- An acknowledged episode displays “acknowledged” and stays out of actionable surfaces until a
  re-notification condition occurs.
- Risk surfaces never render missing factor rows as neutral exposure.
- Qualitative regimes explain transmission and co-movement without a pseudo-precise loss number.

## 9. Rollout and migration

0. Re-fetch and reconcile Git/migration history in an aligned worktree. Prove one Alembic head,
   checkout/runtime/live-DB lineage compatibility, and `describe_drift=None`; take a verified
   backup and prove restore before any live migration or write-path change.
1. Add forward and `legacy_v0` fingerprint computation plus a read-only backfill report. Verify
   WIX maps 34 retained rows to two partial semantic episodes before any consolidating write.
2. Add episode/member/check-receipt schema, compatibility read model, and idempotent evaluator
   persistence; retain raw legacy evaluations and execution telemetry.
3. Migrate every evaluation consumer to the episode read model, then add the shared
   acknowledgement/actionability core and idempotent delivery receipts across web and Telegram.
4. Add the typed decision checkpoint and atomic decision/target/thesis transaction; migrate the
   session writer, then create the current-dated paired repair envelope for decisions 135/136 and
   intent 7.
5. Add the pure regime registry, deterministic classifier, and fallbacks; ship named-scenario
   fallback before factor refresh.
6. Restore business-factor population under the shared DB lock after a dry run proves coverage.
7. Add the WIX retrospective timeline, then wait for holdings evidence before lifecycle closure.
8. Repair and version rate beta independently.
9. Run the WIX/AVDV postmortem only at the frozen horizon and with explicit realized-versus-
   counterfactual status.

Steps 1-9 are blocked by Phase 0 for any migration or live write. A pure registry may be authored
before alignment but cannot be integrated or activated. Each migration requires a dry run,
backup/restore evidence, an idempotency test, compatibility-consumer proof, and a rollback or
forward-repair plan. No step authorizes trade execution.

## 10. Acceptance criteria

1. Running the WIX evaluator ten times with identical evidence creates one episode, increments
   operational duplicate count, and produces no additional prompt.
2. Reordered JSON and different run timestamps produce the same fingerprint.
3. Mechanical thesis metadata changes or provenance-row repointing with unchanged accepted values
   do not create a new forward episode.
4. A new source period, accepted-value restatement, ruleset/evaluator change, or changed resulting
   severity with changed semantic inputs creates a new episode; changed severity with identical
   semantic inputs fails as nondeterminism.
5. Acknowledging an episode suppresses every surface for that episode and does not suppress a
   later materially changed episode.
6. A due review cycle emits one delivery per channel/surface and stays consumed on later runs.
7. Historical WIX backfill yields two `legacy_v0` partial episodes with membership provenance to
   all 34 retained raw rows; no legacy hash is compared with a forward hash.
8. Compatibility tests prove all named evaluation consumers read distinct episodes rather than raw
   repeated runs.
9. Retrying the same source-event checkpoint and payload produces one decision and one sizing
   intent; the same key with a different payload errors and rolls back.
10. A WIX decision can be reconstructed with price, account, size, conviction, thesis state,
   falsifier, alternative, holdings as-of, and prior-owner-decision link.
11. The paired retrospective repair preserves decision timestamps, records AVDV conviction and
   falsifier as `not_provided`, and links decisions 135/136 plus intent 7 atomically.
12. AVDV shows a 2.5444% proposed delta and 4.5%-5.0% target band as separate concepts, with
   `target_unverified` until refreshed holdings reconcile them.
13. A capital-and-attention exit writes no fabricated thesis update.
14. With factor rows absent, WIX still receives partial SaaS/prosumer and duration commentary from
    named-scenario fallback, visibly labeled partial.
15. Risk review uses materialized holdings and shows missing/stale/partial/zero factor states
    distinctly.
16. Golden fixtures produce stable scores, categories, top-two regimes, coverage, and action
    direction across populated, partial, stale, unavailable, offset-heavy, and fallback cases.
17. A 0.10 loading produces materially less vulnerability than an otherwise identical 1.0 loading.
18. The paired WIX sell and AVDV add are evaluated as one synthetic after-state using evidenced ETF
    look-through dimensions; unsupported dimensions remain partial/unavailable and the target band
    remains unverified.
19. Freshness boundary tests cover the instant before, at, and after each source cutoff plus an
    `input_sha` incompatibility inside the nominal age window.
20. A sub-0.50% action or score move below 0.02 without a category crossing yields `no material
    change`.
21. Legacy rate beta cannot appear in owner conclusions until the unit-correct metric is versioned.
22. Unadopted advisor WIX recommendations never enter owner calibration.
23. WIX lifecycle closes only after refreshed holdings evidence the exit.
24. WIX-versus-AVDV comparison is realized only with AVDV execution evidence; otherwise it is
    `counterfactual_not_executed`.
25. Phase 0 blocks every migration/live write when Git, migration, runtime, and DB lineage diverge.
26. No workflow places, stages, or assumes a trade.

## 11. Success measures

- Duplicate owner-facing evaluation episodes per evidence fingerprint: zero.
- Duplicate prompts after acknowledgement: zero.
- Confirmed owner decisions with reconstructable point-in-time basis: at least 95% after rollout.
- Add/trim/hold/sell reviews with explicit factor availability and two qualitative regimes: 100%.
- Owner calibration cohort contamination by unadopted advice: zero.
- Decision records that silently claim an unreconciled target weight: zero.

## 12. Linear delivery slices

The implementation should be tracked as nine mergeable issues with explicit dependencies:

1. **P0 / Urgent:** Reconcile Git/Alembic/runtime/DB lineage and prove backup/restore. This blocks
   every migration and live write-path change below.
2. **P0 / Urgent:** Add semantic evaluation episodes, legacy membership/backfill, and compatibility
   read model. Blocked by 1.
3. **P0 / Urgent:** Add episode acknowledgement, centralized actionability, and delivery receipts.
   Blocked by 2.
4. **P0 / Urgent:** Add atomic idempotent owner checkpoint plus WIX/AVDV paired repair. Blocked by
   1; it blocks 7 and 9.
5. **P1 / High:** Add the versioned deterministic qualitative common-drawdown playbook and review
   integration. Pure registry authoring may start independently; activation is blocked by 1.
6. **P1 / High:** Repair business-factor availability and holdings-native provenance. Blocked by 1
   and the classifier contract in 5.
7. **P1 / High:** Build the WIX learning timeline, owner/advisor separation, and action
   reconciliation. Blocked by 4.
8. **P2 / Medium:** Repair, version, test, and unquarantine 10-year-rate sensitivity. Blocked by 1;
   independent of the postmortem.
9. **P2 / Medium:** Run holdings-confirmed WIX closure and realized-or-counterfactual AVDV
   postmortem. Blocked by 4 and 7 plus future holdings/fill evidence and the frozen horizon.


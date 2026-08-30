# Evaluation Investment Profile — Production Implementation Roadmap

**Status:** Design approved; production scope registered. A local implementation draft exists but is not a merge, deployment, activation, or live-data receipt.
**Approval recorded:** 2026-08-29, by the product owner in the Codex task.
**Approved visual and interaction reference:** `mockups/evaluation_investment_profile_mockup.html`
**Approved artifact SHA-256:** `65a89611c6f30b77a50d87bbcdd1a820f1332081ab958f168c56453feecdb11f`
**Owning surface:** Work OS → Research Engine → Evaluation.
**Linear owner:** `Research workspace UX hardening`; this roadmap succeeds the completed baseline Evaluation issue BHA-69 without reopening it.

## 1. Approval receipt and boundary

Approval covers the shown Evaluation hierarchy, increased global header height,
filterable multi-label investment profiles, the four-level moat scale, direct
valuation and portfolio-impact indicators, and explanation/evidence in the existing
global side peek.

The mockup remains isolated approval evidence. Production must compose the approved
direction through the registered Work OS family, global tokens, controls, and peek
runtime. It must not import or serve prototype markup, copy illustrative values, or
treat a mockup label as a company fact.

The following are explicitly outside this approval:

- a composite Evaluation or Portfolio-fit score on the main surface;
- automatic mutation of an owner-ratified label;
- a moat conclusion inferred from missing evidence;
- a render-time LLM call or a second company-research pipeline;
- portfolio trades, sizing changes, or other brokerage mutations;
- multi-user approval, authentication, tenancy, or public hosting.

## 2. User outcome and smallest coherent behavior

**Primary user:** the owner-researcher triaging operating companies and ETFs under
evaluation.

**Recurring job:** determine the flavor of the investment, the durability and
coverage of its business advantage, its valuation posture, and what it contributes
to or duplicates in the current portfolio before opening deeper research.

**Current workaround:** interpret opaque scalar scores, open several artifacts, and
mentally reconcile thesis, DCF, earnings, factor, and portfolio-fit evidence.

**Dominant action:** scan and filter meaningful labels, then open one contextual side
peek to inspect evidence or ratify/reject the current system suggestion.

**Information order:** company identity → investment profile → business/moat →
portfolio role → valuation → exact research doorway.

**Smallest coherent production slice:** derive company labels from the existing
Investment Decision Card, admitted fundamentals, and current DCF; present them with
direct book indicators in the Evaluation table; expose evidence and fingerprint-
bound owner review in global side peeks; and recompute deterministically after DCF
or source-card changes without silently changing owner belief.

## 3. Ratified vocabulary and label seed

### 3.1 Company Investment Profile Labels

- `long_term_compounder` — Long-term compounder
- `garp` — GARP
- `elite_growth_expensive` — Elite growth / expensive
- `turnaround` — Turnaround
- `narrative_rerating` — Narrative re-rating
- `growth_inflection` — Growth inflection
- `cash_yield_value` — Cash-yield value
- `optionality` — Optionality

A company may carry multiple labels. `garp` and `elite_growth_expensive` are
valuation-owned and deterministic. Other company labels may be suggested by the
current structured Investment Decision Card and must remain source-attributable.

### 3.2 ETF Profile Labels

- `core_beta` — Core beta
- `factor_sleeve` — Factor sleeve
- `thematic_exposure` — Thematic exposure
- `diversifier` — Diversifier
- `defensive_hedge` — Defensive hedge
- `income` — Income
- `tactical_cyclical` — Tactical cyclical

ETF labels describe basket construction and portfolio use. They must not be
inferred from operating-company moat or DCF concepts.

### 3.3 Moat Level

- `multi_business` — Multi-business moat
- `core_business` — Core-business moat
- `narrow_conditional` — Narrow / conditional moat
- `none_demonstrated` — No demonstrated moat

Evidence coverage is independently `sufficient`, `partial`, or `insufficient`.
Insufficient evidence never maps to `none_demonstrated`.

The durable meanings are owned by `DEFINITIONS.md`.

## 4. Existing-capability reuse and net-new functionality

| Capability | Existing authority reused | What it can supply | Net-new work |
|---|---|---|---|
| Brief generation / Investment Decision Card | `src/research/investment_decision_card.py`, persisted current `llm_artifacts` card | qualitative investment flavor, company-specific summary, moat level/rationale/coverage | Extend the structured card and representative eval; do not create a parallel label generator |
| DCF building | current DCF projection and governed DCF doorway | DCF upside and evidence version | Deterministic valuation-label rules and evidence fingerprinting only |
| Earnings/fundamentals analysis | admitted current revenue-growth and FCF-margin projections already used by Evaluation | growth, margin, inflection context, and input completeness | Bind exact current values into the deterministic projection and fail closed when unavailable |
| Portfolio fit/candidate analysis | `allocation.candidate_fit` and its materialized cache | sector exposure, factor tilt, overlap, diversification, and risk-adjusted contribution where present | Present direct indicators and explicit missingness; no replacement composite score |
| ETF workup | existing ETF workup/profile, style loadings, look-through, what-if, and role one-pager | basket, style, concentration, overlap, hedge/diversification evidence | One deterministic ETF Profile Label adapter and confidence/degradation contract |
| Owner belief | no prior Investment Profile Label authority | none | Append-only review ledger with ratify, reject, and retire actions bound to the exact suggestion fingerprint |

Most company functionality is a governed projection over existing research outputs.
The material additions are the owner-review lifecycle and the ETF-specific adapter.

## 5. Truth and state register

| Visible value/state | Canonical authority | Freshness and degradation behavior |
|---|---|---|
| Evaluation membership and instrument kind | canonical `tracked_companies` state and instrument classification | Live request projection. Missing or legacy instrument classification remains explicit; it never changes coverage membership. |
| Company Investment Profile Labels | latest current Investment Decision Card plus deterministic DCF/fundamental rules in `src/research/investment_profile.py` | Derived on uncached hydration. No current structured card yields `Profile pending`; unavailable DCF inputs suppress valuation-owned labels. |
| Label authority state | append-only `investment_profile_label_reviews` | `system_suggested`, `owner_ratified`, or `review_suggested`. A materially changed fingerprint reopens review. |
| Label evidence fingerprint | canonicalized admitted evidence used by the rule or current card | Coarsened where appropriate to avoid churn from immaterial numeric changes. Every mutation must match the current fingerprint. |
| Moat Level and rationale | latest current structured Investment Decision Card | Derived/model-suggested, never owner belief. Coverage and source context are visible in the side peek. |
| DCF upside | current admitted DCF/evaluation projection and exact DCF artifact route | Derived. Missing/stale/degraded DCF remains unavailable or qualified; no synthetic zero. |
| Revenue growth and FCF margin | admitted current fundamentals projection already feeding Evaluation | Derived. Unit/period/source gaps fail closed and cannot satisfy deterministic label rules. |
| Sector/factor/diversification/overlap observations | materialized candidate-fit factors and canonical portfolio snapshot | Derived. Each observation carries direction/availability; missing factors do not collapse into a scalar. |
| Risk-adjusted contribution | candidate-fit what-if / Sharpe delta where current and attributable | Derived. Positive/neutral/negative/unavailable remains visible; it is not a reward promise. |
| ETF Profile Labels | deterministic adapter over current ETF workup and portfolio-impact evidence | Unavailable until the adapter passes fixtures and live representative cases. The UI must say `ETF profile pending` rather than invent labels. |
| Research doorways | current report, DCF, ETF workup, company-desk, and Copilot route resolvers | Only verified routes render as active controls. Missing artifacts are explicit and never direct-URL-only. |
| Filtered count | complete Evaluation response filtered locally | Derived and exact for the hydrated response. Loading/error states do not retain stale counts as current. |

The mockup's sample company values are illustrative. Production values require the
authorities above and the repository's provenance-aware fact resolver.

## 6. Interaction and mutation register

| Control | Intent | Read/write | Success | Empty/error/recovery |
|---|---|---|---|---|
| All / Compounders / GARP / Needs review / ETFs | Restrict the hydrated Evaluation list | Read-only local state | Exact visible rows and count update without another LLM call | Empty state names the active filter; refresh restores current server truth |
| Rationale / Evidence | Inspect company-specific synthesis, label state, moat rationale, and source context | Read-only global side peek | Uses the existing peek focus/dismissal/return contract | Missing evidence renders explicit pending/unavailable copy |
| Impact detail | Inspect direct portfolio indicators and role labels | Read-only global side peek | Shows sector/factor/overlap/diversification/risk-adjusted evidence where available | Missing cache or factor renders per-indicator unavailable state |
| Ratify | Make the current suggestion an owner-ratified label | Append-only write | Persists exact ticker, label, fingerprint, evidence receipt, reviewer, and time; refreshes Evaluation and reopens the peek | Stale fingerprint returns conflict and requires refresh; duplicate Logical Idempotency Key is a no-op |
| Reject | Suppress the exact current suggestion | Append-only write | The same fingerprint remains hidden; material evidence change may suggest review again | Stale fingerprint conflicts; no deletion or overwrite |
| Retire | End an owner-ratified label without erasing its history | Append-only write | Current projection no longer treats the label as owner-ratified | A later materially changed suggestion may reopen review |
| Open DCF / research artifact / Company Desk | Continue to exact deeper evidence | Navigation/overlay only | Existing verified route opens with current ticker context | Missing route is disabled or stated unavailable |
| Refresh | Rehydrate current deterministic truth | Read-only request | Latest card, DCF, fundamentals, candidate-fit, and reviews are projected once | Failure replaces current rows with a truthful unavailable state; no fixtures remain visible |

Mutations are local owner-research classifications, not operations-governance
actions, portfolio trades, or pipeline controls. No Operations surface is added.

## 7. Deterministic DCF and evidence-refresh loop

1. A DCF build or admitted fundamental update changes its current evidence identity.
2. The next uncached Evaluation hydration reads the latest current Investment
   Decision Card, DCF upside, revenue growth, FCF margin, candidate-fit evidence,
   and latest append-only owner reviews through one request-owned connection.
3. Deterministic rules recompute `garp` and `elite_growth_expensive`; qualitative
   labels are projected from the current card without regenerating it.
4. Equal evidence fingerprints preserve `owner_ratified` or exact rejection.
5. A materially different fingerprint returns a previously ratified label to
   `review_suggested`; removal of its supporting rule does the same with
   `suggested=false`.
6. The UI refreshes the table and any open side peek. It never mutates the card or
   silently rewrites owner belief.
7. A review click carrying a stale fingerprint fails with HTTP 409 and instructs a
   fresh read.

No scheduled label-refresh job or cached suggestion table is required. The current
projection is rebuildable from durable research artifacts, admitted inputs, and the
append-only owner-review ledger.

## 8. Implementation boundary census

| Boundary | Disposition | Exact seam / proof |
|---|---|---|
| Canonical vocabulary | **Keep/ratified** | `DEFINITIONS.md`: Investment Profile Label, ETF Profile Label, and Moat Level. |
| Typed label/rule projection | **Add** | `src/research/investment_profile.py`; deterministic rules, evidence fingerprints, review resolution, and current projection. |
| Investment Decision Card | **Change** | `src/research/investment_decision_card.py`, `src/llm/prompt_versions.py`, and `evals/rubrics/investment_decision_card.md`; qualitative labels and moat only, with valuation labels reserved for code. |
| Durable state | **Add, append-only** | Alembic `0034_add_investment_profile_label_reviews.py`; review identity/history only. Suggestions remain derived. |
| Evaluation read model | **Change** | `src/pipeline/work_os_evaluation.py`; versioned v2 payload with profile, fundamentals, and direct portfolio indicators. |
| Main Evaluation composition | **Replace** | `src/pipeline/work_os_shell.py`; remove visible scalar scores, add label filters and registered side-peek doorways. |
| Side peeks | **Compose existing global family** | `src/pipeline/peeks.py` and `execution/comments_server_content_routes.py`; no new overlay grammar. |
| Review writer | **Add** | fingerprint-bound POST route in `execution/comments_server.py`; append-only, idempotent, no card mutation. |
| ETF classifier | **Add** | deterministic adapter over existing ETF workup/candidate-fit evidence; no company-label reuse. Exact owning module selected during slice discovery. |
| Copilot | **Keep existing runtime** | Contextual prompts may receive the same typed profile projection; no second Copilot or render-time call. |
| Header height | **Change global master** | `src/ui/tokens.py` and generated design-system/golden mirrors; 56px shared rule. |
| Design registry | **Change** | Register the final Work OS dynamic visual digest and preserve the existing family. |
| Legacy scalar fields | **Temporary compatibility seam** | Removed from frontend immediately. Delete six v1 compatibility fields after 2026-09-28 unless an evidence-backed design explicitly restores them. |
| Jobs | **No change** | Brief, DCF, earnings, and candidate-fit jobs remain the producing authorities. No label refresh scheduler. |
| Operations | **No-surface-change disposition** | Research classification and owner review do not represent service, source, pipeline, or portfolio-operation health. |
| Prototype | **Keep isolated** | `mockups/evaluation_investment_profile_mockup.html`; never imported by production. |

## 9. Delivery roadmap and Linear slices

### Slice A — Company profile contract and deterministic refresh

1. Land canonical vocabulary, typed labels, four-level moat scale, evidence coverage,
   fingerprints, and current projection.
2. Extend the Investment Decision Card schema/prompt/rubric and fallback behavior.
3. Add deterministic DCF/fundamental rules and prove missing-input behavior.
4. Add the append-only review migration and projection semantics.

**Exit gate:** schema/migration/rule tests pass; valuation labels cannot be authored
by the model; equal evidence preserves owner state and material changes reopen
review.

### Slice B — Approved Evaluation surface and owner-review peeks

1. Version the Evaluation read model and expose company profile plus direct book
   indicators.
2. Recompose the production table and filters through registered Work OS controls.
3. Add global profile and portfolio-impact peeks plus fingerprint-bound owner review.
4. Remove scalar scores from the frontend and preserve honest loading/empty/error
   states.

**Exit gate:** the primary scan → inspect → ratify/reject path works at desktop and
narrow widths; no mock values or composite scores render; stale actions conflict.

### Slice C — ETF profile and book-impact completeness

1. Inventory the exact ETF workup, style-loading, look-through, concentration,
   factor, overlap, and what-if fields available without new collection.
2. Define deterministic, explainable multi-label rules with an unavailable state and
   fixture provenance.
3. Connect ETF labels and evidence to the same Evaluation filters and side-peek
   pattern without applying company moat/DCF concepts.
4. Prove direct portfolio indicators remain truthful with missing/stale candidate-fit
   data.

**Exit gate:** representative ETF fixtures classify reproducibly; every label has an
evidence explanation; unsupported cases remain pending.

### Slice D — Representative evaluation, release, and pruning

1. Run the Investment Decision Card live evaluation on a representative set covering
   compounder, GARP, expensive elite growth, turnaround, rerating, weak/no moat, and
   insufficient evidence.
2. Measure owner ratification/rejection and revise only through the versioned prompt,
   rule, and eval loop.
3. Run merge-facing tests, golden regeneration, design sync, desktop/narrow browser
   checks, migration upgrade/rollback rehearsal on a disposable Mac database, and
   production-shaped Windows read verification before activation.
4. Delete the six legacy scalar compatibility fields and superseded v1 tests by
   2026-09-28 unless explicitly restored.

**Exit gate:** representative eval meets its rubric, all deterministic and rendered
gates pass, activation is receipt-backed, and the legacy scalar seam is either
deleted or explicitly reauthorized.

## 10. Acceptance trace

| ID | Acceptance outcome | Primary proof |
|---|---|---|
| EIP-01 | The main Evaluation surface shows no composite Evaluation or Portfolio-fit score. | DOM/source assertion and rendered desktop/narrow evidence. |
| EIP-02 | Company labels are multi-label, filterable, and visibly system-suggested, owner-ratified, or review-suggested. | Projection and UI tests plus populated render. |
| EIP-03 | Moat uses exactly four durability levels and separately reports insufficient evidence. | Pydantic/schema tests and representative cards. |
| EIP-04 | Rationale, company-specific context, moat evidence, and portfolio-impact detail use the existing global side peek. | Route, focus/dismissal, keyboard, and screenshot evidence. |
| EIP-05 | DCF/fundamental updates deterministically refresh valuation labels without overwriting owner belief. | Fingerprint-transition and stale-action tests. |
| EIP-06 | Company qualitative labels reuse the Investment Decision Card/brief and earnings corpus; no parallel generator exists. | Import/caller census, prompt version, and live eval receipts. |
| EIP-07 | Direct sector, factor, overlap, diversification, and risk-adjusted observations replace obfuscated fit claims where evidence exists. | Candidate-fit fixtures and per-indicator missingness tests. |
| EIP-08 | ETF labels are deterministic, explainable, multi-label, and unavailable when evidence is inadequate. | ETF adapter fixtures and populated/degraded UI states. |
| EIP-09 | Owner review is append-only, idempotent, exact-evidence-bound, and recoverable through refresh. | Migration triggers, API tests, and 409 conflict path. |
| EIP-10 | Production preserves the approved information order, registered family, 56px header, responsive hierarchy, and truthful empty/error states. | Design registry, golden, route canary, keyboard, console, and network evidence. |

## 11. Release, rollback, and deletion matrix

| Change | Release gate | Rollback/recovery |
|---|---|---|
| Structured card v2 | Schema/fallback tests and representative eval | Existing cards remain readable through the backward-compatible unavailable default; revert generation version without deleting artifacts. |
| Review migration 0033 | Clean upgrade plus append-only trigger tests on disposable DB | Leave the additive table in place if UI rolls back; never delete owner history during emergency rollback. |
| Evaluation v2/UI | Route contract, populated/degraded fixtures, desktop/narrow render | Revert the UI/read-model consumer together; do not display fixture rows or silently fall back to scores. |
| Review POST | Idempotency, stale conflict, input validation, and receipt tests | Disable/remove the mutation doorway while retaining persisted reviews. |
| ETF classifier | Representative fixtures and missing-data cases | Return `ETF profile pending`; do not fall back to company labels. |
| Legacy scalar deletion | No approved consumer or owner restoration by 2026-09-28 | Removal is source/API cleanup only after reachability proof; retained historical calculations remain untouched. |
| Windows activation | Exact live host, migration head, route hydration, and source/freshness checks | Restore prior application build; additive review rows remain valid and reconstructable. |

## 12. Learning and kill criteria

The cheapest usefulness signal is the append-only owner-review ledger: within 30
days of activation, the owner should be able to review a meaningful cross-section of
Evaluation names without returning to scalar scores.

- If fewer than 25% of active Evaluation companies receive any ratify/reject action,
  simplify the label set or interaction before adding more labels.
- If more than 40% of reviewed suggestions are rejected, hold expansion and revise
  the prompt/rules against the rejected evidence set.
- If insufficient moat evidence dominates representative cases, improve source/card
  completeness before changing the moat labels.
- If ETF rules cannot explain their output from existing ETF workup and portfolio
  evidence, keep ETF profile pending and do not add a probabilistic classifier.
- Unless explicitly restored by owner decision, remove the legacy scalar fields by
  2026-09-28.

## 13. Verification baseline and remaining gaps

Current local draft evidence recorded before roadmap registration:

- targeted implementation suite: 268 tests passed before the final fingerprint edge
  adjustment; all 10 investment-profile tests passed afterward;
- Ruff and strict Pyright passed for the changed profile and Evaluation modules;
- workspace golden tests and the desktop/narrow canonical route matrix passed;
- design registry/canonical conformance tests passed;
- direct automation of the isolated `file://` mockup was blocked by browser security,
  while the owner inspected and approved that exact local artifact in the app.

Remaining required evidence:

- live representative Investment Decision Card evaluation;
- deterministic ETF classifier fixtures and production wiring;
- production-rendered browser verification of every populated, empty, error, focus,
  overflow, and stale-review state;
- clean repository-wide design-sync after reconciling the unrelated unregistered
  `execution/comments_server.py` worktree surface;
- production-shaped Windows migration/read/activation receipts.

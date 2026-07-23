# Personal Investment Partner — Product Requirements Document

**Status:** Product requirements ratified by the owner on 2026-07-23; implementation not started.
**Audience:** Repository owner and implementation agents.
**Scope:** Umbrella PRD. P0 and P1 are implementation-ready; P2 and P3 define the intended direction and acceptance boundary.
**Document authority:** This is a design artifact, not a Layer-1 directive. It does not authorize edits to `directives/`. Any directive patch identified here requires separate owner authorization, validation, and commit authorization under the repository rules.

---

## 1. Executive summary

The platform already contains most of the analytical and advisory machinery needed to
act as a personal investment partner. It has portfolio analytics, next-dollar scoring,
candidate-fit analysis, risk and stress modules, full evaluation workspaces, thesis and
KPI tracking, owner-profile facts, a Worldview, Ask, proactive standups, Telegram
capture, decisions, outcome grading, and a unified decision journal.

The product problem is not missing machinery. It is that the machinery produces too
many adjacent outputs, asks for more process than a hobby investor will maintain, and
does not consistently turn its context into one personalized recommendation.

This program consolidates the existing components around three owner objectives:

1. Track investments and portfolio risk and decide where the Incremental Dollar should
   go.
2. Evaluate new investments with a robust, scalable, and consistent process.
3. Improve as an investor through an Owner Decision journal and a candid Senior Partner
   Brief that understands the portfolio, directional theses, KPIs, capacity, risk
   posture, Worldview, and decision history.

The product must approach the hobby with excellence while fitting a normal attention
budget of approximately five hours per week. It may surface more work during earnings
clusters, new-position diligence, or a portfolio shift, but it must not convert rigor
into compulsory hedge-fund ceremony.

The primary changes are:

- Replace competing final allocation rankings with one hybrid deterministic/LLM
  Incremental Dollar Recommendation.
- Make the Risk Budget reliable and historical.
- Replace the global 8% concentration flag with Concentration Zones and an explicit
  hold-versus-trim assessment beginning at 12%.
- Put one Investment Decision Card at the top of every evaluation workspace.
- Reduce Discovery to a focused research queue instead of a 300-name firehose.
- Consolidate proactive advice into one Senior Partner Brief.
- Make the journal owner-first and make Telegram/phone capture flexible, asynchronous,
  and confirmation-gated.
- Demote redundant workflows before removing dead code after parity.

The platform remains pull-only with respect to brokerage execution. It never places,
routes, or stages a trade.

---

## 2. Product intent

### 2.1 Product promise

At any important investment moment, the platform should be able to say:

> This is what I think you should do, why I think it, what I am uncertain about, what
> would disprove the hypothesis, how it fits this portfolio, and what deserves your
> limited research time next.

That answer must be:

- grounded in current portfolio and research data;
- specific enough to recommend add, hold, trim, pass, wait, or retain cash;
- candid about uncertainty and missing evidence;
- personalized without treating unconfirmed inferences as facts;
- traceable to sources and the point-in-time context used;
- clearly separated from trade execution; and
- usable without completing a new checklist or maintaining a second research system.

### 2.2 Hobby-scale excellence

The owner enjoys investing because it is difficult and intellectually interesting.
The product should preserve that challenge while removing administrative work.

Normal-week design target:

- no more than three priority decisions in the weekly briefing;
- an effort estimate on each suggested activity;
- approximately five hours of total suggested work;
- no requirement to visit every surface;
- no repeated request for information already present in thesis, KPI, portfolio, owner
  profile, or decision history.

Active-week behavior:

- automatically recognize earnings clusters, an active new-position evaluation, or a
  portfolio shift;
- show the incremental effort and why it is warranted;
- allow the suggested workload to exceed five hours;
- do not increase the proactive notification frequency merely because more work exists.

### 2.3 Product posture

The platform recommends; the owner decides.

Recommendations must use language such as “This is my preferred plan,” “I think,” and
“The main reason I could be wrong is...” rather than presenting a model output as fact.
Numeric probabilities are permitted only when based on a defensible base rate,
calibrated history, or an explicit scenario model. Otherwise use a verbal confidence
label and describe the uncertainty directly.

---

## 3. Goals and non-goals

### 3.1 Goals

#### G1 — One coherent capital-allocation answer

For a stated amount of new cash, produce one preferred plan, retain cash as a
first-class option, and show the best alternative and best diversifier.

#### G2 — Decision-facing portfolio risk

Explain the portfolio's current Risk Budget, its material changes, the holdings and
shared drivers consuming it, and the incremental effect of the preferred plan.

#### G3 — Consistent new-investment evaluation

Make every evaluation conclude with the same source-grounded Investment Decision Card,
without requiring a user-authored initiation memo or a parallel diligence folder.

#### G4 — Scalable research prioritization

Reduce Discovery to a small set of ideas worth attention now, preserve the rest without
requiring triage, and make the next research action obvious.

#### G5 — A useful senior partner

Provide one proactive voice that connects portfolio, research, owner context, and past
choices instead of issuing disconnected pings and memos.

#### G6 — A journal that teaches

Make Owner Decisions the primary learning unit, capture them with minimal friction,
separate process quality from outcome, and keep unadopted advisor views out of the
owner's decision history.

### 3.2 Non-goals

This program does not build:

- brokerage connectivity, trade execution, order staging, or automated rebalancing;
- a multi-user product, public advice surface, or externally shareable personalized
  recommendation;
- a native mobile application;
- a new top-level Advisor navigation destination;
- a mandatory frozen earnings setup;
- a full catalyst register or hedge book;
- an investment-committee gate;
- required target weights for every holding;
- a daily task ritual;
- a universal numeric confidence or probability score;
- a new thesis/KPI tracker that duplicates `micro_thesis/holdings`, `thesis_state`,
  decision conditions, or the existing KPI system;
- a new owner-memory store that duplicates `owner_profile_facts`, `positioning_intents`,
  Worldview, or the decision journal; or
- a recommendation based on stale, outlier, or silently missing required data.

---

## 4. Current-state findings

This section records the evidence that determined the product design. Counts are from
the read-only repository and database scan performed on 2026-07-22.

### 4.1 Existing product inventory

| Capability | Existing implementation | Current limitation |
| --- | --- | --- |
| Held-name next-dollar model | `src/allocation/model.py::build_next_dollar_model` | Scores current holdings only and emits a softmax distribution. |
| Candidate portfolio fit | `src/allocation/candidate_fit.py` and `data/candidate_fit.json` | Produces a portfolio-fit view separate from the held-name allocation answer. |
| Candidate what-if | `src/allocation/what_if.py` | Pro-rata funding only; fixed weights stop at 8%. |
| Evaluation attractiveness | `src/pipeline/research_cockpit.py` | Uses a different valuation/growth/FCF/PEG ranking from allocation and fit. |
| Advisor next-dollar memo | `src/advisor/memos.py::_next_dollar_model_rows` | Another ranking path; current next-dollar memos are not structurally scoreable. |
| Position review | `src/advisor/position_review.py` | Strong deterministic/LLM/guard pipeline, but its global `CONCENTRATION_PCT = 8.0` is too blunt. |
| Portfolio risk | `src/portfolio_risk.py`, `src/risk_reward.py`, stress, correlation, crowding, thesis-collision modules | Analytical depth exists, but risk persistence is not reliable enough for drift coaching. |
| Risk persistence | `src/portfolio_risk_snapshot_store.py` and migrations 0105/0185 | The latest row had null core metrics and the history table had no rows. Writes are coupled to rendering. |
| Portfolio IA | `src/pipeline/portfolio_console_panel.py` | Portfolio → Allocation leads with Positioning and Performance, while the next-dollar answer is in Health/Synthesis. |
| Discovery | `src/discovery/`, `src/pipeline/discovery_panel.py`, `execution/discovery_build.py` | 362 candidates were `new`; the panel cap does not create an actionable funnel. |
| Evaluation reports | `src/report/` and `src/report/renderers/workspace_html.py` | Rich evidence, but no single consistent conclusion/disposition artifact. |
| Old diligence path | `execution/start_diligence.py`, `build_diligence.py`, `check_initiation_gate.py` | Disconnected from `discovery_build`; required diligence directory is absent. |
| Owner context | `src/owner_profile/`, `src/positioning/`, Worldview, Ask anchors | The substrate exists; appetite/positioning adoption is thin and the current form is high-friction. |
| Proactive advice | `src/standup/`, `src/research/governor.py`, `src/pipeline/weekly_packet.py` | Several parallel initiation mechanisms compete for attention. |
| Telegram | `src/capture/telegram.py`, `src/capture/poller.py`, voice transcription, reply/callback cores | Flexible transport exists, but decision fill-in still expects a rigid two-line answer. |
| Journal | `decisions`, `v_decision_journal`, grading, calibration panels | The default record mixes 64 advisor decisions with 24 Owner Decisions; all 88 had null `process_quality`. |

### 4.2 Data-state implications

- Active tracked universe: 11 portfolio, 37 evaluation, and 41 watchlist companies.
- Evaluation coverage: 18 of 37 had an authored non-stub thesis; 13 latest evaluation
  DCFs were marked as outliers.
- Watchlist coverage: 9 of 41 had an authored thesis; 21 latest DCFs were outliers.
- Portfolio coverage: all 11 holdings had authored theses and usable DCFs; no latest
  portfolio DCF was flagged as an outlier.
- Candidate fit was current for all 37 evaluation names.
- Discovery held 362 `new`, 2 `building`, 0 `built`, and 10 `dismissed` rows.
- The decision ledger held 88 rows: 64 advisor and 24 owner.
- Every decision had null `process_quality`.
- Only one journal row clearly carried linked or prior advice.
- `positioning_intents` had no rows.
- Owner-profile capacity was substantially populated, but no appetite fact was affirmed.
- The Worldview was no longer starved: it held 9 Tenets, 10 themes, and 8 stances.
- The current risk snapshot could not support a valid time-series comparison.

The product should therefore consolidate and circulate existing context, not start
another context-gathering program.

---

## 5. Product principles

### P1 — Decision first

Every primary surface should answer a user decision. Supporting metrics remain
available, but they do not lead.

### P2 — Derive before asking

Use holdings, thesis, KPIs, DCF, candidate fit, owner profile, Worldview, tracker
transactions, and decision history before requesting input. Ask only for a material
correction or final disposition.

### P3 — Probabilistic humility

Every recommendation states:

- the preferred action;
- the central hypothesis;
- the main evidence;
- the main unknowns;
- what would disprove or materially weaken the hypothesis;
- the relevant alternative; and
- the confidence basis.

No UI should equate a precise score with truth.

### P4 — Facts are deterministic; judgment is governed

Eligibility, source freshness, units, portfolio arithmetic, concentration classification,
risk deltas, constraint enforcement, and recommendation validation are deterministic.
A governed LLM may choose and explain a preferred plan only from the eligible frontier.

### P5 — Soft inference, hard affirmation

Observed portfolio facts may inform analysis immediately. Inferred owner preferences may
be shown as a proposed Portfolio Posture, but only owner-affirmed limits may block a
recommendation.

### P6 — Preserve original evidence

Raw Telegram text/voice transcription, source documents, original underwriting, prior
thesis state, prior advice, and prior Owner Decisions remain append-only and
point-in-time queryable.

### P7 — One action core, multiple surfaces

Web, mobile Inbox, Telegram callbacks, and Ask commands must call the same typed action
functions. No surface owns a second implementation of the state transition.

### P8 — No ceremony without expected value

The owner is never required to freeze an earnings setup, maintain a catalyst database,
fill a target book, or complete a diligence checklist simply to receive advice.

---

## 6. Information architecture

No new top-level navigation is introduced.

| Existing destination | Required role after this program |
| --- | --- |
| **Today** | Senior Partner Brief, active-week context, and pending confirmation count. |
| **Companies → Discovery** | Focused top-ten research queue with compare/build/dismiss actions. |
| **Evaluation workspace** | Investment Decision Card before the detailed report tabs. |
| **Holding workspace** | Existing thesis/KPI/report experience plus links into relevant allocation and partner context. |
| **Portfolio → Health** | Thesis health, portfolio risks, and red-team analysis; remove the primary next-dollar answer. |
| **Portfolio → Allocation** | Incremental Dollar Recommendation, Risk Budget, Portfolio Posture, what-if, then Performance. |
| **Portfolio → Record** | Historical advice artifacts, advanced audit views, and operational records. |
| **Review → Ledger** | Owner-first decision journal, lessons, unresolved confirmation drafts, and calibration. |
| **Ask** | Conversational continuation for every recommendation/card/brief. |
| **Mobile Inbox** | Compact Tailscale-protected review and confirmation surface; no full mobile redesign. |
| **Telegram** | Fast capture, voice, concise recommendations, callbacks, and deep links; no total portfolio value or tax-lot detail. |

Existing deep links and old panel IDs must continue to resolve through the aliasing
pattern in `src/pipeline/command_center_shell.py`.

---

## 7. P0 — Portfolio allocation and risk coherence

### 7.1 P0-A: valid Risk Budget history

#### User outcome

The owner can see current portfolio risk, what changed, which holdings/shared drivers
caused the change, and whether an Incremental Dollar plan improves or worsens it.

#### Backend requirements

1. Add a single-purpose execution entry point:
   `execution/refresh_portfolio_risk_snapshot.py`.
2. Reuse the calculation and persistence primitives in:
   - `src/pipeline/portfolio_panel.py::_build_risk_snapshot`;
   - `src/portfolio_risk_snapshot_store.py`;
   - existing portfolio analytics and tracker clients.
3. Move authoritative persistence out of GET/render paths. Rendering may read and
   display snapshots but may not be the only writer.
4. Run the refresh after tracker/portfolio analytics are available in the morning
   pipeline. The step must honor the same pipeline/run lock as other writers.
5. Validate a `RiskBudgetSnapshot` Pydantic model before writing. At minimum:
   - `as_of` is present;
   - position count is positive;
   - current weights are finite and plausibly normalized;
   - core concentration and downside fields are not all null;
   - currency-bearing fields carry a currency;
   - source freshness is explicit.
6. An invalid refresh must:
   - not overwrite the latest valid snapshot;
   - not append an invalid history row;
   - persist/log an explicit failed ingestion event;
   - leave the UI on the last valid snapshot with an age and failure reason.
7. A valid refresh upserts `portfolio_risk_snapshots` and appends idempotently to
   `portfolio_risk_snapshot_history`.
8. The idempotency key is `{user_id}_{portfolio_as_of}_{analytics_input_sha}`.
9. Historical comparisons use snapshots produced by the same metric/version definition.
   A metric-version change must be explicit and must not render a false delta against an
   incomparable prior.

#### Decision-facing Risk Budget

The primary Risk Budget shows no more than four categories:

1. Single-name concentration and Concentration Zones.
2. Correlated/shared-driver exposure, including thesis collision where available.
3. Downside/stress and drawdown posture.
4. Capacity/liquidity context and any owner-affirmed hard limit.

Secondary details may include factor, sector, geographic, macro, and return-efficiency
metrics behind an expansion or peek.

#### Frontend requirements

Portfolio → Allocation renders:

- current state;
- change since the prior valid snapshot;
- top two drivers;
- snapshot age and source status;
- impact of the preferred Incremental Dollar plan; and
- “Inspect drivers” and “Compare before/after” actions.

Portfolio → Health may retain the detailed risk and red-team panels.

#### Acceptance criteria

- A valid morning run produces one latest row and one history row.
- Re-running the same input does not duplicate history.
- A partial tracker response cannot replace the last valid snapshot.
- The UI never presents null metrics as zero.
- A stale snapshot is visibly stale.
- At least 30 consecutive valid daily snapshots can be queried and compared.

### 7.2 P0-B: Concentration Zones

#### Policy

The global 8% concentration rule is replaced with the following default soft zones:

| Post-trade single-name weight | Classification | Required treatment |
| --- | --- | --- |
| `<10%` | Ordinary | Weight alone creates no concern. |
| `10% to <12%` | Meaningful | Show contribution to portfolio risk when adding; no trim implication. |
| `12% to <15%` | Concentrated | Trigger an explicit hold-versus-trim assessment/conversation. |
| `15% to <20%` | Highly concentrated | Stronger trim/diversification discussion and a high evidence hurdle for adding. |
| `>=20%` | Exceptional | Full comparison of hold, trim, and diversification alternatives. |

Rules:

1. A Concentration Zone never forces a trim.
2. A trim recommendation requires at least one additional reason:
   - thesis impairment;
   - unattractive forward risk/reward;
   - excessive shared-driver/correlation exposure;
   - owner capacity or liquidity pressure;
   - an owner-affirmed hard position limit; or
   - a demonstrably superior alternative after tax and portfolio impact.
3. Appreciation-driven drift receives more tolerance than an intentional add.
4. New buying faces progressively higher evidence and portfolio-fit requirements as the
   resulting weight enters higher zones.
5. Taxes inform the comparison but do not automatically determine it.
6. A healthy, not-overvalued winner remains protected from a price-only trim by the
   behavioral guard unless the multi-factor assessment supports reducing it.
7. An owner-affirmed hard maximum supersedes the soft defaults and must be shown as an
   affirmed constraint.

#### Backend disposition

- **Replace:** `src/advisor/position_review.py::CONCENTRATION_PCT`.
- **Augment:** `PreAnalysis` with zone, entry method (`appreciation_drift` versus
  `intentional_add` where determinable), risk contribution, and assessment reasons.
- **Augment:** `src/advisor/position_tax.py::propose_trim_fraction`; a soft zone may
  propose a comparison amount but may not assume the zone boundary is a required target.
- **Augment:** the behavioral guard so zone status alone is insufficient to bypass the
  winner-protection rule.
- **Augment:** `src/allocation/what_if.py`.

#### What-if requirements

The current fixed `(1, 2, 3, 5, 8)%` menu must support at least:

`1, 2, 3, 5, 8, 10, 12, 15, 20, 25%`.

The API must not silently clamp a requested weight to 8%. It must either:

- evaluate the exact validated weight, with the cache key rounded to a documented
  precision; or
- return a validation error with the supported presets.

What-if adds a `funding_mode`:

- `new_cash` — the P0 default; new cash increases the portfolio denominator and does not
  pretend existing positions were sold pro rata;
- `pro_rata_reallocation` — the existing model, retained as an advanced comparison.

### 7.3 P0-C: Decision-ready eligibility

#### Eligible universe

The preferred Incremental Dollar Recommendation may consider:

- active holdings; and
- active evaluation names that pass the evidence gate.

It may not prefer:

- raw Discovery candidates;
- watchlist-only names without a current evaluation;
- a name with an outlier or unusable valuation;
- a name without candidate portfolio-fit analysis;
- a name without an explicit directional hypothesis and disconfirmers; or
- a name whose required market/research inputs are materially stale.

Cash is always eligible.

#### Deterministic gate

Create `src/allocation/eligibility.py` with a typed `DecisionReadyAssessment`.

Required checks:

1. Current price under the existing market-data freshness policy.
2. Latest usable DCF/valuation with no blocking `sanity_flag`.
3. Core financial/KPI coverage sufficient to explain the hypothesis.
4. Candidate-fit result against the current book.
5. Explicit directional hypothesis.
6. At least one disconfirming condition or evidence category.
7. Portfolio context and resulting-weight calculation.
8. Source provenance and as-of dates.

A system-drafted hypothesis is permitted but is labeled as such. A user-authored thesis
is not a prerequisite.

The assessment returns:

- `eligible`;
- `blocking_reasons`;
- `warning_reasons`;
- `source_freshness`;
- `hypothesis_origin`;
- `portfolio_fit_status`; and
- the input hashes required for reproducibility.

The LLM cannot override `eligible=False`.

### 7.4 P0-D: Incremental Dollar Recommendation

#### User input

- amount of new cash;
- optional decision horizon;
- optional user correction to current Portfolio Posture; and
- optional mode preference for exploration, without changing the primary recommendation.

The amount must be explicit. Omitted cash may show a percentage/lens preview, but it
cannot be labeled a deployable plan.

#### Deterministic frontier

Create `src/allocation/recommendation.py` as the deep module that composes existing
components without duplicating their math.

Inputs include:

- `NextDollarModel` factors for held names;
- candidate-fit and what-if results for evaluation names;
- DCF/valuation and data-quality flags;
- thesis/KPI evidence;
- current portfolio weights and risk snapshot;
- Concentration Zones;
- current Portfolio Posture;
- affirmed capacity and appetite facts;
- relevant Worldview/Tenets;
- prior Owner Decisions and behavioral patterns; and
- cash as a candidate.

The deterministic layer constructs a small frontier rather than one opaque global
score. At minimum it identifies:

- best balanced plan;
- highest expected-return plan;
- best diversifier;
- retain-cash case; and
- ineligible names with blockers.

Existing component scores remain visible as evidence but cease being separate final
answers.

#### Governed judgment

The governed LLM receives only:

- eligible frontier plans;
- typed portfolio and owner context;
- evidence-quality labels;
- explicit unknowns;
- source references; and
- validation constraints.

It selects one preferred plan and explains why it wins. It may recommend:

- deploy all new cash;
- deploy part and retain part;
- retain all cash; or
- defer because no available plan clears the evidence/risk hurdle.

The structured output is `IncrementalDollarRecommendation` and includes:

- as-of date and input hash;
- recommendation status;
- preferred plan with dollars, percentages, and resulting weights;
- best alternative;
- best diversifier;
- cash retained;
- central hypothesis;
- why the answer is personalized;
- main supporting evidence;
- main unknowns;
- disconfirming evidence;
- bull/base/bear or equivalent scenario reasoning;
- verbal confidence and its basis;
- required follow-up research;
- eligible frontier identifiers;
- source references;
- risk-snapshot reference;
- engine/prompt versions; and
- whether the result is LLM-selected or deterministic fallback.

#### Validation

Before persistence:

- allocations are finite and non-negative;
- allocated plus retained cash equals the supplied cash within currency rounding;
- every security was eligible in the exact input snapshot;
- resulting weights recompute correctly;
- owner-affirmed hard limits are respected;
- concentration assessment is included when resulting weight is 12% or more;
- no source identifier is invented;
- required humility fields are non-empty; and
- confidence language is not converted into an unsupported numeric probability.

#### Persistence

Persist the structured recommendation in `llm_artifacts`:

- `scope='portfolio'`;
- `purpose='incremental_dollar_recommendation'`;
- `content_json` = validated structured output;
- `content_md` = user-facing render;
- `input_sha256` = deterministic context hash;
- normal prompt-version, model, source-parent, LLM-call, dirty, expiry, and supersession
  behavior.

An unadopted recommendation is an advice artifact, not a decision.

Add a nullable `decisions.source_artifact_id` FK to `llm_artifacts.id` so that:

- “Save,” an executed matching action, or “Hold this view accountable” can link the
  resulting decision to the point-in-time artifact;
- `v_decision_journal` can show the exact advice available before an Owner Decision; and
- historical advisor decisions remain intact.

#### Deterministic fallback

If the LLM call fails, is budget-blocked, or fails structured validation:

- preserve the deterministic frontier;
- show the balanced frontier plan as a mechanical fallback only if it passes all
  deterministic requirements;
- label it `deterministic_fallback`;
- show no synthesized confidence;
- never persist an empty artifact disguised as “no recommendation.”

#### Frontend

Portfolio → Allocation leads with:

1. Cash input.
2. Preferred plan.
3. “Why this plan.”
4. Main uncertainty and disconfirming evidence.
5. Best alternative and best diversifier.
6. Resulting Risk Budget.
7. Actions:
   - Compare;
   - Change amount;
   - Simulate weight;
   - Ask why;
   - Save as provisional intent;
   - Hold this view accountable; and
   - Dismiss.

The primary card does not expose a weighted-factor spreadsheet. A details expansion
shows component scores, data freshness, frontier construction, and provenance.

Compare opens a consistent, deterministic view for up to three securities across:

- expected-return/valuation inputs;
- evidence quality and freshness;
- directional hypothesis;
- main disconfirming case;
- current and resulting weight;
- candidate portfolio fit;
- Concentration Zone;
- marginal Risk Budget effect; and
- open research blockers.

The comparison may ask the governed LLM to summarize the tradeoff through Ask, but the
comparison table itself does not require a new LLM call.

### 7.5 P0-E: Portfolio Posture

#### Purpose

Replace the current detailed positioning form as the default entry point with a short,
correctable description of the current book.

Example shape:

> Growth-oriented and concentrated, with long-duration capacity and meaningful shared
> exposure across two drivers. Current behavior suggests tolerance for volatility but
> limited appetite for adding to already-correlated positions.

#### Requirements

- Derive observed book facts from live portfolio data.
- Use only affirmed owner-profile facts as stated owner constraints.
- Treat inferred appetite/behavior as a proposal, not an asserted fact.
- Show “Mostly right” and “Adjust” actions.
- On confirmation, save through the existing versioned `positioning_intents` and/or
  owner-profile fact stores; do not create a parallel profile table.
- Keep target volatility, Sharpe floor, sleeve, and sector controls under Advanced.
- Do not require a maintained target for every holding.
- Propose a position range only when an active decision needs one.

---

## 8. P1 — Consistent new-investment evaluation

### 8.1 P1-A: Investment Decision Card

#### User outcome

Opening an evaluation immediately answers:

- What must be true?
- What is the evidence?
- What does the security appear to price?
- How would it affect this portfolio?
- What is the strongest credible reason not to proceed?
- What is missing?
- What should I do with the idea now?

#### Backend

Create `src/research/investment_decision_card.py`.

The typed `InvestmentDecisionCard` separates:

1. **Company hypothesis**
   - directional thesis;
   - operating mechanism;
   - key KPIs;
   - current confirming/disconfirming evidence.
2. **Security setup**
   - current price/as-of;
   - valuation and scenario range;
   - what appears priced in;
   - valuation/data-quality caveats.
3. **Portfolio fit**
   - expected role;
   - candidate-fit result;
   - correlated/shared exposure;
   - expected Concentration Zone if funded.
4. **Disconfirming case**
   - strongest bear hypothesis;
   - evidence that would make it right;
   - next proof point.
5. **Evidence readiness**
   - available source classes;
   - stale/missing inputs;
   - Decision-ready Security status and blockers.
6. **Suggested disposition**
   - `pass`;
   - `watch`;
   - `research_further`; or
   - `promote`.
7. **Uncertainty**
   - verbal confidence;
   - why that confidence is justified;
   - what would change the disposition.

Inputs reuse:

- report section models;
- `evaluation_snapshot`;
- DCF runs and sanity flags;
- authored/system thesis;
- KPI facts and thesis evaluation;
- bear case;
- comparable set where available;
- candidate fit/what-if;
- source provenance; and
- current Portfolio Posture/Risk Budget.

Persist in `llm_artifacts`:

- `scope='ticker'`;
- `purpose='investment_decision_card'`;
- `ticker=<TICKER>`;
- `content_json` = validated card;
- `content_md` = rendered summary;
- normal input-sha, prompt-version, provenance, dirty, and supersession semantics.

Generate or refresh:

- at the end of a successful `execution/discovery_build.py` evaluation build;
- when a blocking parent artifact becomes dirty;
- when a material source refresh changes the input hash; or
- on explicit user refresh.

Do not run an LLM on every workspace GET.

#### Disposition semantics

The user-facing actions are:

- **Pass** — preserve the card and evidence, archive the active evaluation, and record an
  Owner Decision.
- **Watch** — move/retain the security in the watchlist state with the card preserved and
  record an Owner Decision.
- **Research further** — create or update a bounded `research_tasks` item; this is not an
  Owner Decision.
- **Promote** — mark the security as an active allocation candidate while retaining
  `list_type='evaluation'`; record an Owner Decision. It does not claim the security is
  owned.

Actual portfolio membership continues to follow portfolio-tracker holdings rather than
an evaluation button.

Extend the decision vocabulary and renderers to admit `pass`, `watch`, and `promote`.
Reconciliation must treat them as research dispositions, not assumed brokerage
transactions.

#### Frontend

The card renders before the evaluation tab bar or as the first Overview content. It
must not bury the report.

Primary actions:

- Pass;
- Watch;
- Research further;
- Promote;
- Compare;
- Ask the Senior Partner;
- Edit/correct hypothesis.

Detailed report tabs remain the evidence base. The card links directly to the relevant
section rather than duplicating it.

#### Acceptance criteria

- Every newly built evaluation has a card or an explicit card-generation failure.
- Every claim carries a source reference or is labeled judgment.
- Company hypothesis, security setup, and portfolio fit are distinct.
- An outlier DCF cannot produce `Decision-ready Security=true`.
- No disposition occurs from a model suggestion without an owner action.
- Rebuilding unchanged inputs reuses the current artifact.

### 8.2 P1-B: focused Discovery

#### User outcome

Discovery answers “Which ideas are worth my limited research time now?” rather than
showing a long undifferentiated queue.

#### Ranking

Augment existing discovery scoring with decision-useful annotations:

- source signal strength and corroboration;
- recency;
- basic portfolio adjacency/overlap;
- evidence readiness;
- likely research effort; and
- first rejection reason.

Do not pretend a raw Discovery name has full candidate-fit or valuation precision.

The primary view shows at most ten candidates. The rest remain preserved in the
database and available under “More candidates”; they do not require disposition.

#### Candidate card

Each top candidate shows:

- why it surfaced now;
- preliminary hypothesis;
- likely portfolio role or overlap;
- first rejection risk;
- evidence currently available;
- estimated effort to reach a useful conclusion; and
- next workflow.

Actions:

- Build evaluation;
- Compare;
- Dismiss;
- Watch; and
- Open evidence.

#### Backend disposition

- **Augment:** `src/discovery/scoring.py` and `score_json`.
- **Augment:** `src/discovery/store.py` read models.
- **Augment:** `src/pipeline/discovery_panel.py`.
- **Keep:** existing source signals and state machine.
- **Do not add initially:** a second discovery queue table.
- **Do not automatically delete or dismiss:** candidates outside the top ten.

### 8.3 P1-C: retire the disconnected diligence conclusion

After Investment Decision Card parity:

- remove links and documentation that direct users to
  `execution/start_diligence.py`, `execution/build_diligence.py`, and
  `execution/check_initiation_gate.py`;
- preserve any historical artifacts;
- verify no active caller remains;
- remove dead code only in the P3 cleanup PR after repository-wide search and tests.

The Investment Decision Card replaces their conclusion role, not the research data in
the evaluation workspace.

---

## 9. P2 — Senior partner and learning loop

### 9.1 P2-A: Senior Partner Brief

#### Product behavior

The Senior Partner Brief becomes the one primary proactive advisory experience.

It has five ordered sections:

1. What changed that matters.
2. The highest-priority portfolio decision.
3. Best current use of incremental capital.
4. One assumption or behavioral pattern worth challenging.
5. One prior Owner Decision worth revisiting.

The brief distinguishes:

- action requested;
- context only;
- blocked by missing evidence; and
- no action warranted.

Normal weeks contain at most three action-requesting items. Additional developments
remain context.

#### Triggering

Weekly:

- compose one synthesis from the weekly packet, current portfolio/risk state, active
  evaluations, open Owner Decisions, and material research changes.

Immediate:

- likely thesis break;
- materially changed portfolio risk;
- failed/stale critical data that invalidates active advice; or
- a time-sensitive decision the owner explicitly marked active.

Everything else waits for the weekly brief.

Retain the existing governor:

- no more than one proactive ping per day;
- retain weekly caps and dismiss/mute learning;
- suppressed items may enter the weekly brief;
- an active week increases visible context, not ping frequency.

#### Backend

Create `src/advisor/senior_partner_brief.py` as a composition layer over:

- `src/standup/`;
- `src/pipeline/weekly_packet.py`;
- Ask evidence packs and citations;
- the latest Incremental Dollar Recommendation;
- the latest valid Risk Budget;
- Investment Decision Cards;
- Worldview and owner profile;
- calibration/decision history; and
- current research proposals/open loops.

Reuse:

- `standup_messages` for delivery/dedup/cooldown;
- the rolling Ask session;
- `llm_artifacts` with `scope='portfolio'` and `purpose='senior_partner_brief'` as the
  canonical versioned structured brief;
- existing Telegram callbacks and weekly packet action core.

The existing “Analyst standup” session may be renamed in UI copy only after the
canonical session/deep-link compatibility is preserved.

#### Frontend and actions

Today:

- brief summary;
- effort estimate per action;
- active-week explanation when applicable;
- Why?;
- Compare alternatives;
- Correct context;
- Record/confirm decision;
- Defer; and
- Dismiss.

Telegram:

- concise ticker/action/rationale/confidence;
- no total portfolio value, exact account balance, or tax-lot detail;
- buttons for Why, Review in Inbox, Defer, and Dismiss;
- deep link into the Tailscale-protected mobile page.

Ask:

- opens with the exact point-in-time brief/recommendation context;
- preserves citations and source posture;
- corrections flow back through typed actions rather than free-form hidden mutation.

### 9.2 P2-B: flexible Telegram and mobile Inbox

#### Existing transport to retain

- Telegram long polling;
- offset/dedup behavior;
- text, URL, document, and voice capture;
- voice transcription;
- reply-to-card routing;
- inline keyboards;
- pending-reply support;
- original note storage and provenance;
- best-effort confirmations that never block capture.

#### New behavior

Free-form text or voice may express:

- an executed position change;
- a Pass/Watch/Promote disposition;
- a decision rationale;
- a correction to prior context;
- a request/question; or
- an ordinary musing.

The system first lands the raw capture through the existing capture path. It then
produces a typed Decision Draft (`DecisionDraft`) asynchronously. The LLM must never be on the
must-succeed raw capture path.

The Decision Draft includes:

- source note/update/message identifiers;
- original text and transcription provenance;
- interpreted intent;
- ticker candidates;
- proposed action/disposition;
- proposed amount/weight when stated;
- proposed rationale;
- proposed link to existing advice/card;
- parse confidence;
- ambiguity/questions;
- prompt/model/run metadata;
- status; and
- expiry.

#### New persistence

Add `decision_drafts` with a closed status machine:

`captured -> parsed -> awaiting_confirmation -> confirmed|corrected|dismissed|expired`

Requirements:

- unique source identity/idempotency key;
- FK/backlink to the raw `analyst_notes` row where available;
- `draft_json` validated before persistence;
- original text never overwritten;
- a parse failure leaves the original capture intact;
- confirming/correcting is the only path that creates or updates an Owner Decision;
- reprocessing the same source does not duplicate a decision;
- consequential owner-profile changes use the existing proposal/affirmation gate rather
  than direct mutation.

#### Mobile Inbox

Add a compact responsive route in `execution/comments_server.py`, protected by the
existing Tailscale/private-host assumptions.

The route is not a second application. It contains:

- pending Decision Draft confirmations;
- unresolved Investment Decision Card dispositions;
- the latest Senior Partner Brief;
- short recommendation/card views;
- Confirm, Correct, Dismiss, Defer, and Open full app actions.

All controls use `src/ui/controls.py`. The mobile page shares action cores with desktop
and Telegram.

#### Replacement of rigid decision nudge

Demote the current two-line “conviction / what would prove you wrong?” requirement in
`src/capture/decision_nudge.py`.

New default:

- prefill rationale and disconfirmers from the relevant thesis, KPIs, decision
  conditions, recommendation, and raw capture;
- ask the owner to correct only if material;
- allow one free-form reply or voice note;
- place ambiguity in the mobile Inbox;
- do not require a conviction rating or falsifier merely to preserve a decision.

Existing explicit falsifiers remain valuable and may be requested when genuinely
missing from the thesis, but they are not a compulsory form field on every action.

### 9.3 P2-C: owner-first decision journal

#### Default view

Review → Ledger defaults to Owner Decisions.

Separate filters:

- Owner Decisions;
- adopted/gradeable advisor views;
- all advice artifacts;
- process lessons; and
- operational coaching history.

Historical advisor decisions are preserved but no longer dominate the default timeline.

#### Capture

Owner Decisions include:

- tracker-detected executed position changes;
- explicit Pass;
- explicit Watch; and
- explicit Promote.

Fix `src/research/decision_capture.py`:

- remove the hard-coded `_ROSTER`;
- resolve valid tickers from active `tracked_companies` and instrument aliases;
- preserve the existing ticker-matcher ambiguity behavior;
- ensure evaluation/watchlist names can be captured;
- keep the raw source link.

An advisor recommendation becomes a gradeable decision only when:

- the owner saves/adopts it;
- a matching executed action is reconciled to it; or
- the owner explicitly selects “Hold this view accountable.”

`execution/record_decisions.py` must stop turning every machine recommendation into a
decision by default. Unadopted views remain artifacts.

#### Process reflection

At a natural review point, ask:

- Sound process;
- Flawed process; or
- Need more evidence.

Map to the current storage carefully:

- Sound process -> `process_quality='sound'`;
- Flawed process -> `process_quality='flawed'`;
- Need more evidence -> leave null and schedule no repeated pestering;
- `lucky` remains an outcome-aware calibration label, not the user-facing synonym for
  sound process.

The system may propose a process label from evidence but the owner can correct it.
Outcome and process remain separate axes.

#### Coaching output

The journal should surface lessons such as:

- right thesis, poor sizing;
- wrong conclusion, sound process;
- correct outcome for the wrong reason;
- repeated early exit from an intact winner;
- repeated underweighting of the owner's strongest evidence;
- advice that improved or harmed a decision.

The system must not infer a stable behavioral owner-profile fact from one event. Derived
patterns continue through the existing propose/affirm pathway.

---

## 10. LLM architecture and governance

### 10.1 New purposes

Register purpose keys in `src/llm/cli.py::LLM_MODELS` and
`src/llm/prompt_versions.py`:

- `incremental_dollar_recommendation`;
- `investment_decision_card`;
- `senior_partner_brief`;
- `decision_draft_parse`; and
- corresponding rubric/judge purpose only if the existing generic eval judge cannot
  own it.

Model IDs are not selected in this PRD. Each purpose must enter through the existing
model-picker/eval loop and earn the cheapest-at-parity model.

### 10.2 One entry point

All calls use the existing governed `src/llm/cli.py::call_llm` or
`src/llm/structured.py::call_llm_structured`.

No feature module imports a provider SDK or invokes a model directly.

### 10.3 Structured schemas

Use Pydantic for:

- `IncrementalDollarRecommendation`;
- `InvestmentDecisionCard`;
- `SeniorPartnerBrief`;
- `DecisionDraft`; and
- any LLM judge output.

Structured parsing follows the existing repair-once behavior. A second validation
failure raises and records a failed call; it never becomes `{}`, `[]`, or `None`
masquerading as a valid empty result.

### 10.4 Logging and budgets

Every call records:

- purpose;
- prompt version;
- model/provider/transport;
- input/output/cached/reasoning tokens where available;
- public-list cost estimate;
- latency;
- success/failure;
- fallback;
- run ID;
- artifact or draft linkage; and
- error class.

Each new purpose requires a monthly budget row and explicit `warn`, `skip`, or `block`
behavior.

New scheduled LLM work must:

- use per-item degrade;
- never block the raw Telegram capture;
- avoid the 03:00–05:00 America/Los_Angeles protected pipeline window unless it is an
  existing registered leg;
- register its cadence/window in `directives/llm_quota_scheduling.md` only after
  separate owner authorization.

### 10.5 Evaluation plan

#### Incremental Dollar Recommendation

Use a rubric plus deterministic invariant suite.

Rubric facets:

- recommendation usefulness;
- portfolio personalization;
- probabilistic humility;
- evidence grounding;
- alternative quality;
- disconfirming case;
- consistency with the deterministic frontier;
- no institutional-process overreach.

High-stakes minimum sample for downgrade decisions: at least 10 representative
portfolio states.

Deterministic cases include:

- no eligible security -> retain cash;
- one eligible security;
- preferred high-return name violates affirmed max;
- resulting position enters each Concentration Zone;
- appreciation drift versus intentional add;
- DCF outlier;
- missing candidate fit;
- stale risk snapshot;
- strong diversifier with lower expected return;
- LLM tries to select an ineligible ticker;
- allocations do not sum to cash;
- confidence expressed as unsupported probability.

#### Investment Decision Card

Golden/structural checks:

- required sections;
- company/security/portfolio separation;
- no `Decision-ready Security` status when a blocking input is missing;
- source IDs resolve;
- no disposition mutation;
- disconfirming case is present;
- input changes invalidate the artifact.

Rubric checks:

- hypothesis clarity;
- priced-in reasoning;
- strength of opposing case;
- decision usefulness;
- uncertainty honesty.

#### Decision Draft

Golden set:

- executed buy/sell text;
- Pass/Watch/Promote language;
- ticker ambiguity;
- question versus decision;
- voice-transcription noise;
- dollar versus percent amounts;
- correction of prior context;
- ordinary musing false positives;
- adversarial/prompt-injection text inside captured content.

The parser must prefer an ambiguous draft over a false consequential mutation.

#### Senior Partner Brief

Rubric:

- prioritization under the attention budget;
- materiality;
- integration across portfolio/research/history;
- actionable but humble recommendation;
- no duplicated items;
- no more than three normal-week actions;
- respect for notification policy.

### 10.6 Fallback and hard-stop behavior

- Authentication failure: halt the affected LLM leg immediately and surface it.
- Budget block: show deterministic data and explicit unavailability; do not swallow it
  with provider fallback.
- Transient operational failure: use the centrally configured fallback, then per-item
  defer.
- Schema failure: repair once; then fail loudly and retain prior valid artifact.
- Missing/stale decision input: do not call the LLM as a substitute for evidence.

---

## 11. Data contracts and provenance

### 11.1 Currency and units

- Every dollar field carries currency.
- Percent fields state whether the code uses `[0,1]` fraction or `[0,100]` percentage.
- UI renders portfolio weights as percentage points.
- New-cash amounts do not silently include account cash unless the user explicitly
  supplies or confirms it.

### 11.2 Time

Every recommendation/card/brief records:

- generated time;
- market-data as-of;
- portfolio as-of;
- risk-snapshot as-of;
- valuation as-of;
- relevant source dates; and
- expiry/refresh policy.

“Current” is never inferred from file modification time alone when a typed source date
exists.

### 11.3 Point-in-time reproducibility

The stored artifact must allow the system to explain:

- what the portfolio looked like;
- what evidence was available;
- which owner-profile facts were affirmed;
- which Portfolio Posture version was used;
- which engine/prompt/model version ran; and
- why the recommendation changed.

Use input hashes, artifact parent/source IDs, snapshot references, and supersession
chains rather than copying the entire database state into prose.

### 11.4 Privacy

Telegram may show:

- ticker;
- action;
- recommended percentages;
- rationale;
- verbal confidence;
- uncertainty;
- disconfirmers; and
- deep links.

Telegram must omit by default:

- total portfolio value;
- exact account balances;
- tax-lot detail;
- household income/capacity amounts; and
- secrets or local filesystem paths.

Full sensitive detail remains in the Tailscale-protected web surface.

### 11.5 Required schema changes

#### `decisions`

Add:

- `source_artifact_id INTEGER NULL REFERENCES llm_artifacts(id) ON DELETE SET NULL`.

Extend the application recommendation vocabulary to include:

- `pass`;
- `watch`; and
- `promote`.

These three kinds are research dispositions. Transaction reconciliation must never map
them to a presumed buy or sell.

#### `decision_drafts`

Create a typed table with:

- `id INTEGER PRIMARY KEY`;
- `user_id TEXT NOT NULL`;
- `source_note_id INTEGER NULL`;
- `source_channel TEXT NOT NULL`;
- `source_external_id TEXT NULL`;
- `idempotency_key TEXT NOT NULL UNIQUE`;
- `original_text TEXT NOT NULL`;
- `transcription_json TEXT NULL`;
- `draft_json TEXT NULL`;
- `parse_confidence REAL NULL`;
- `status TEXT NOT NULL`;
- `prompt_version TEXT NULL`;
- `model TEXT NULL`;
- `llm_call_id INTEGER NULL`;
- `decision_id INTEGER NULL`;
- `expires_at TEXT NULL`;
- `created_at TEXT NOT NULL`;
- `updated_at TEXT NOT NULL`;
- `confirmed_at TEXT NULL`; and
- `dismissed_at TEXT NULL`.

Constraints:

- `status` is closed under
  `captured|parsed|awaiting_confirmation|confirmed|corrected|dismissed|expired|parse_failed`;
- JSON columns are null or `json_valid`;
- `parse_confidence` is null or within `[0,1]`;
- a confirmed/corrected row has a `decision_id`;
- the source note and LLM call use `ON DELETE SET NULL`;
- the created Owner Decision is never deleted by deleting a draft.

#### `v_decision_journal`

Rebuild the view to expose:

- source artifact purpose and timestamp;
- whether advice preceded the Owner Decision;
- the Decision Draft/source note;
- research-disposition kind;
- existing memo, coach, profile, nudge, outcome, and process fields.

The SQL view remains neutral. Owner-first behavior belongs in the renderer/query default,
not in a view that makes advisor history inaccessible.

#### Existing tables reused without duplication

- `llm_artifacts` — Incremental Dollar Recommendation, Investment Decision Card, and
  Senior Partner Brief.
- `portfolio_risk_snapshots` and `portfolio_risk_snapshot_history` — Risk Budget state.
- `positioning_intents` and `owner_profile_facts` — Portfolio Posture confirmation and
  affirmed limits.
- `research_tasks` — Research further.
- `standup_messages`, `weekly_packet_runs`, `weekly_packet_items`, `coach_pings`, and
  `coach_mutes` — proactive delivery/governance.
- `analyst_notes` — immutable raw mobile/Telegram provenance.

### 11.6 Private HTTP and action-core contracts

The internal Flask surface may use the following routes, following existing CSRF,
origin, user, and ticker validation:

| Route | Method | Responsibility |
| --- | --- | --- |
| `/api/allocation/recommendation` | `GET` | Read the latest current artifact and its freshness. |
| `/api/allocation/recommendation` | `POST` | Generate/refresh for an explicit cash amount; enqueue/return an action ID if the governed call is long-running. |
| `/api/allocation/recommendation/<id>/adopt` | `POST` | Save provisional intent or create a linked accountable/adopted decision. |
| `/api/allocation/compare` | `POST` | Deterministic comparison of up to three validated tickers and a cash amount. |
| `/api/investment-decision-card/<ticker>` | `GET` | Read the current card and readiness status. |
| `/api/investment-decision-card/<ticker>/disposition` | `POST` | Apply Pass/Watch/Research further/Promote through one action core. |
| `/api/portfolio-posture/confirm` | `POST` | Confirm or correct the proposed Portfolio Posture through existing stores. |
| `/api/decision-drafts` | `GET` | Read pending drafts for the Inbox. |
| `/api/decision-drafts/<id>/confirm` | `POST` | Confirm a draft and create/link one Owner Decision idempotently. |
| `/api/decision-drafts/<id>/correct` | `POST` | Validate corrected fields, then create/link one Owner Decision. |
| `/api/decision-drafts/<id>/dismiss` | `POST` | Dismiss the draft without deleting the raw capture. |
| `/mobile/inbox` | `GET` | Render the compact private mobile review surface. |

Route handlers remain thin. Shared typed service functions own validation and state
transitions so Telegram callbacks and desktop/mobile web cannot drift.

---

## 12. Frontend behavior and design-system requirements

### 12.1 Component discipline

All new HTML surfaces:

- use tokens from `src/ui/tokens.py`;
- use controls/chips/pills/wells/ticker labels from `src/ui/controls.py`;
- use `ui.prose.render_prose` for rendered prose;
- add layout only in surface CSS;
- preserve JS-hook classes alongside kit classes;
- add any new token-emitting `src/**.py` file to
  `tests/test_ui_controls.py::REGISTERED`; and
- maintain keyboard access, visible focus, mobile 16px input floors, and semantic
  headings.

### 12.2 Loading and failure states

Every new panel has:

- loading state;
- empty/not-applicable state;
- stale state;
- failed refresh state;
- last-valid state; and
- a clear recovery action.

Do not collapse “failed,” “not generated,” “ineligible,” and “no action warranted” into
the same empty card.

### 12.3 Explanation hierarchy

Primary view:

- recommendation;
- why;
- uncertainty;
- user action.

Secondary view:

- alternatives;
- scenarios;
- portfolio/risk impact;
- evidence gaps.

Advanced/audit:

- component calculations;
- source graph;
- model/prompt/version;
- stale-input details;
- historical artifacts.

---

## 13. Success measures

### 13.1 Product outcome measures

| Objective | Measure |
| --- | --- |
| Coherent allocation | The same current artifact ID and preferred plan render on Today, Portfolio → Allocation, mobile Inbox, Telegram summary, and Ask context. |
| Risk reliability | On tracker-available days, a valid risk refresh succeeds; invalid input never replaces the last valid snapshot. |
| Decision readiness | Every active evaluation clearly reports eligible or blocked, with reasons. |
| Research focus | The default Discovery view never requests attention on more than ten names. |
| Attention budget | A normal weekly brief requests at most three actions and includes effort estimates totaling approximately five hours or less. |
| Journal quality | Review defaults to Owner Decisions; unadopted advice does not create decision rows. |
| Capture coverage | At least 80% of tracker-detected executed position changes produce a confirmable draft or explicit unmatched status. |
| Process learning | Process-quality reflection becomes populated for a meaningful share of matured Owner Decisions without compulsory forms. |
| Advice linkage | Adopted advice is point-in-time linked to the resulting Owner Decision. |
| Humility | Eval suite rejects unsupported certainty, missing disconfirmers, and recommendations outside the eligible frontier. |

### 13.2 Anti-metrics

Do not optimize for:

- number of pings;
- number of memos;
- number of journal rows;
- number of researched tickers;
- time spent in the application;
- percentage of recommendations adopted; or
- forced completion of profile/thesis fields.

The existing “coach changed a decision” measure may remain as evidence, but it must not
incentivize more aggressive advice.

---

## 14. Implementation map

### 14.1 Replace, augment, create

| Capability | Replace | Augment | Create |
| --- | --- | --- | --- |
| Incremental Dollar Recommendation | Separate final rankings in next-dollar memo and evaluation attractiveness; primary next-dollar panel in Health | `src/allocation/model.py`, candidate fit, what-if, thesis/KPI/DCF/risk/owner context | `src/allocation/eligibility.py`, `src/allocation/recommendation.py`, structured artifact purpose |
| Risk Budget | Render-triggered persistence as authoritative writer | `portfolio_risk_snapshot_store.py`, morning pipeline, risk renderers | `execution/refresh_portfolio_risk_snapshot.py`, typed validation model |
| Concentration Zones | `CONCENTRATION_PCT = 8.0`; implicit trim-to-8 behavior; what-if cap | Position review, tax comparison, behavioral guard, what-if | `src/allocation/concentration.py` as the shared policy module |
| Portfolio Posture | Detailed positioning form as default | `positioning_intents`, owner profile, current portfolio | Lightweight derivation/confirmation read model |
| Investment Decision Card | Disconnected diligence conclusion/gate | Report builders, thesis, DCF, candidate fit, provenance | `src/research/investment_decision_card.py`, artifact purpose |
| Focused Discovery | Default long candidate list | Discovery scorer/store/panel | No table initially; focused read model |
| Senior Partner Brief | Multiple competing primary pings/memos/weekly packet experiences | Standup, weekly packet, Ask, governor, Telegram | `src/advisor/senior_partner_brief.py`, structured brief schema |
| Mobile decision capture | Rigid two-line fill-in as default | Telegram poller, pending replies, voice, Inbox rendering | `decision_drafts`, parser purpose, shared confirmation action |
| Owner-first journal | Mixed advisor/owner default; hard-coded capture roster | `decisions`, reconciliation, `v_decision_journal`, Review renderer | Advice-artifact link and new research disposition kinds |

### 14.2 Proposed delivery order

#### P0.1 — Risk truth

- Snapshot validation.
- Scheduled writer.
- Last-good/staleness UI.
- Historical delta tests.

No LLM dependency.

#### P0.2 — Shared concentration policy

- Concentration Zone module.
- Position review/behavioral guard/tax updates.
- What-if weight/funding changes.
- Regression tests.

No new LLM dependency.

#### P0.3 — Eligibility and deterministic frontier

- Decision-ready gate.
- Unified candidate adapter across holdings/evaluations/cash.
- Cash-funded portfolio math.
- Deterministic frontier and invariant tests.

No LLM dependency.

#### P0.4 — Governed recommendation and Allocation UX

- Structured purpose/schema/evals/budget.
- Artifact persistence.
- Portfolio → Allocation composition.
- Ask/Telegram/mobile summary reads.
- Advice-to-decision linking.

#### P1.1 — Investment Decision Card

- Structured card.
- Build/dirty integration.
- Evaluation workspace render.
- Disposition actions.

#### P1.2 — Discovery focus

- Ranking annotations.
- Top-ten view.
- Compare/build/dismiss/watch actions.

#### P2.1 — Decision Draft and mobile Inbox

- Raw-first capture.
- Async parser.
- Confirmation state machine.
- Dynamic roster.
- Owner-first journal default.

#### P2.2 — Senior Partner Brief

- Consolidated composition.
- Weekly and exceptional trigger delivery.
- Today/Telegram/Ask/mobile wiring.
- Retire competing primary pings.

#### P3 — Cleanup

- Demote advanced/redundant screens.
- Validate replacement parity and deep-link compatibility.
- Remove dead diligence and legacy next-dollar generation paths.
- Preserve historical data.

### 14.3 PR boundaries

Each PR should own one vertical seam and remain reversible:

1. Risk persistence.
2. Concentration policy.
3. Eligibility/frontier.
4. Recommendation artifact/evals.
5. Allocation UX.
6. Decision Card backend/evals.
7. Decision Card UX/dispositions.
8. Discovery focus.
9. Decision Draft backend/Telegram.
10. Mobile Inbox/journal.
11. Senior Partner Brief.
12. Cleanup.

Do not combine schema, LLM prompt, multiple UI surfaces, and dead-code deletion in one
unreviewable PR.

### 14.4 Execution-layer constraints

Every new `execution/` entry point must:

- be single-purpose;
- use typed `argparse` or Typer arguments;
- validate external and cross-process inputs/outputs with Pydantic;
- use a deterministic idempotency key;
- write structured JSON events to stderr;
- keep machine-readable stdout separate from logs;
- write payloads above the repository size threshold to `.tmp/<task_id>/`;
- checkpoint multi-step state for exact resumption;
- use the shared network/session/retry/redaction primitives;
- classify transient, contract/schema, and authentication failures before retrying; and
- acquire or honor the shared run lock before mutation.

`execution/comments_server.py` and the orchestration layer must not contain allocation,
risk, eligibility, decision-state, or parsing business logic.

---

## 15. Test and verification plan

### 15.1 Unit tests

- Eligibility reason matrix.
- Concentration Zone boundaries.
- Appreciation drift versus intentional add.
- Cash-funded and pro-rata what-if math.
- Allocation-sum and resulting-weight invariants.
- Risk snapshot validation/idempotency.
- Artifact input-hash reuse/supersession.
- Decision Draft status transitions and dedup.
- Advisor artifact remains out of `decisions` until adoption.
- Dynamic ticker universe.
- Disposition-to-list-state mapping.
- Telegram privacy redaction.

### 15.2 Integration tests

- Morning analytics -> valid snapshot -> history -> Risk Budget delta.
- Discovery build -> evaluation -> Investment Decision Card.
- Incremental cash input -> eligibility -> frontier -> LLM selection -> artifact -> render.
- Artifact adoption -> Owner Decision -> journal link.
- Tracker transaction -> Decision Draft -> mobile confirm -> decision reconciliation.
- Telegram voice -> raw capture -> parsed draft -> Inbox correction.
- Senior Partner Brief -> Telegram callback -> shared web action core.
- LLM failure/budget block -> last-valid or deterministic fallback.

### 15.3 UI tests

Any frontend PR runs:

```text
python -m pytest tests/test_ui_controls.py -q
```

Report-renderer changes also run:

```text
GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py
```

The implementer must review the golden diff rather than accepting it mechanically.

Additional required coverage:

- mobile viewport;
- keyboard-only actions;
- focus return after confirmation;
- stale/failure/empty states;
- old hash/deep-link redirects;
- no duplicate controls;
- no raw component reinvention.

### 15.4 Migration tests

- Upgrade from the current production-like schema.
- Existing advisor/owner decisions remain readable.
- `v_decision_journal` rebuilds and downgrades safely.
- New recommendation kinds do not break reconciliation.
- Decision Draft constraints and idempotency.
- Advice-artifact link preserves rows on artifact deletion/supersession via safe FK
  behavior.

### 15.5 Operational verification

- Capture poller restarts after deployment.
- Comments server restarts after deployment.
- Report artifacts regenerate where required.
- Scheduled jobs respect run locks and quota windows.
- Daily-chain verification includes risk refresh status.
- No new secrets or sensitive payloads enter logs.

---

## 16. Failure modes

| Failure | Required behavior |
| --- | --- |
| Tracker unavailable | Use last valid portfolio/risk state with age; do not call it current. |
| Partial tracker payload | Fail snapshot validation; preserve last valid. |
| No Decision-ready Security | Prefer retain cash and list the highest-value blockers. |
| DCF outlier | Block preferred allocation; allow research/card discussion with warning. |
| Missing candidate fit | Block preferred allocation, not the whole evaluation workspace. |
| LLM auth failure | Halt the affected leg and surface configuration; no retry loop. |
| LLM transient failure | Central fallback, then per-item defer; preserve deterministic frontier/raw capture. |
| Structured-output failure | Repair once, then fail loudly; retain prior valid artifact. |
| Telegram send failure | Preserve web/DB state; retry according to existing best-effort policy without duplicate mutation. |
| Decision Draft ambiguity | Await confirmation; never infer a consequential mutation. |
| Source becomes dirty | Mark dependent artifact dirty; show last generated state with reason until refreshed. |
| Owner profile fact is proposed | May be shown as a proposal; cannot block or be quoted as affirmed preference. |
| Recommendation becomes stale before action | Require refresh or explicit acknowledgment; preserve the original artifact for audit. |

---

## 17. Directive changes requiring separate authorization

Implementation will likely require consolidated patches to:

- `directives/next_dollar_model.md` — replace the final-answer contract while preserving
  reusable factors;
- `directives/navigation_ia.md` — move the allocation decision to Portfolio → Allocation
  and define the mobile Inbox/Senior Partner Brief placement;
- `directives/llm_quota_scheduling.md` — register any new scheduled LLM leg/window;
- any existing directive that explicitly mandates the global 8% concentration behavior,
  rigid decision nudge, or old diligence gate.

The implementation agent must:

1. identify exact conflicting clauses with evidence;
2. propose a consolidated patch;
3. request owner authorization before editing;
4. validate the resulting procedure; and
5. request separate authorization before committing the directive edit.

This PRD itself does not modify those directives.

---

## 18. Definition of done

The program is complete when:

1. Portfolio → Allocation produces one current, reproducible Incremental Dollar
   Recommendation over cash, holdings, and eligible evaluations.
2. The recommendation is candid, personalized, uncertainty-aware, and cannot escape the
   deterministic frontier.
3. Risk snapshots are valid, historical, and written independently of rendering.
4. The global 8% rule is gone; Concentration Zones and the 12% trim-assessment threshold
   work consistently across review, tax comparison, behavioral guard, allocation, and
   what-if.
5. Every active evaluation has an Investment Decision Card or explicit blocker.
6. Discovery defaults to a top-ten attention queue.
7. Today, Telegram, mobile Inbox, and Ask share one Senior Partner Brief and action core.
8. Flexible Telegram text/voice produces raw-preserved, confirmation-gated
   Decision Drafts.
9. Review defaults to Owner Decisions, adopted advice is linked, and unadopted advice
   does not pollute the journal.
10. Normal-week suggested work remains hobby-scaled.
11. All LLM purposes are routed, schema-validated, logged, budgeted, and eval-gated.
12. Redundant workflows are demoted first and removed only after parity, migration, and
   test completion.

---

## 19. Ratified owner decisions

The following requirements are closed and should not be reopened during implementation
without new evidence:

- One umbrella PRD with implementation-ready P0/P1 and directional P2/P3.
- Explicit recommendations with humility and no fake certainty.
- Holdings plus Decision-ready evaluation names compete for new cash.
- Cash is a first-class candidate.
- Event-triggered exceptional advice plus one weekly synthesis.
- Owner Decisions include executed changes and Pass/Watch/Promote dispositions.
- Normal attention budget is approximately five hours per week, with adaptive active
  weeks.
- One preferred plan plus best alternative and best diversifier.
- Telegram remains the primary mobile capture/conversation surface.
- A compact Tailscale-protected mobile Inbox supplements Telegram.
- Free-form text/voice may be LLM-parsed, but consequential mutation requires
  confirmation/correction.
- Inferred preferences are soft; only affirmed limits block.
- Every evaluation candidate must pass the evidence gate before preferred allocation.
- Advice artifacts do not become journal decisions until adopted, acted upon, or
  explicitly held accountable.
- Telegram omits total portfolio value, exact balances, and tax-lot details by default.
- Proactive interruptions remain rare and governed.
- Appreciation-driven winners receive more tolerance than intentional adds.
- New cash is not silently mixed with sell-funded swaps in P0.
- Position ranges are proposed only when a live decision needs them.
- No new top-level navigation.
- Redundant workflows are demoted before dead code is removed.

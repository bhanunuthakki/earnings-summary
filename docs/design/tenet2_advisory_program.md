# Tenet-2 Advisory Program — Strategy & Phased Roadmap

**Status:** strategy proposal, 2026-07-17. No code changes accompany this document.
**Owner decisions required before build:** see §7 (the decision list). Nothing in this
document is authorized until the owner rules on §7.
**Scope guard (restated, non-negotiable):** this platform never executes trades and never
gives regulated personalized financial advice from any external-facing surface. It is a
self-owned decision-support tool for its single owner. Everything below stays inside the
existing advisor posture: Socratic coaching, grounded facts, stances only on request,
"the owner decides."

---

## 1. The strategic frame

The program's two tenets (owner, July 2026):

1. **The research fortress** — deterministic data, provenance, extraction, calculations,
   with an LLM layer on top. Large active build program (bottoms-up metrics engine,
   segment quarterly framework, comparable sets — see §6 for the collision map).
2. **The advisor** — advising the owner on how holdings/candidates fit into *their*
   portfolio at *this* time, and improving their decision-making around portfolio, life
   circumstances, and risk appetite.

The standing verdict on tenet 2 is the 2026-07-02 red-team's "**fortress with no
inhabitants**," and the standing success bar is owner decision #8: **the coach must
change ≥1 real portfolio decision by end of Q3'26**, measured by the attestation-gated
Coach P&L counter (silence never counts).

### 1.1 What the inventory actually found (the surprise)

A full four-track inventory (advisor package, quant fit/risk machinery, Ledger decision
loop, owner-context stores) establishes something the "build tenet 2" framing gets
backwards: **the delivery and measurement layers are mostly built. The missing layer is
the owner-context substrate itself.**

What already exists and works:

- **Pull coaching:** `/review <T>` → deterministic PreAnalysis → governed LLM verdict →
  deterministic behavioral guard that already enforces the owner's one empirically
  confirmed bias (sell-winners-too-early; 5 graded wrong sells: MU, GOOGL, TSM, NVDA,
  AMZN), with tax cost as corroborating rationale (`src/advisor/position_review.py`).
- **Push coaching:** pre-buy pledge → catalyst-test challenge; retro net for unannounced
  fills; decision-stub nudges (`decision_nudges`, nudged at most once ever); governed
  coach pings (`coach_pings` + `coach_mutes`: freshness gate, ≤1/day ≤3/week, 3-strike
  auto-mute); Sunday "N need you" packet on both web and Telegram.
- **Measurement:** `decisions` with owner/advisor attribution, falsifiers, grading and a
  separate `process_quality` axis; `stance_scores` (P2.5); `predictions` grading; monthly
  calibration scorecard with an honest min-n floor; the attestation-gated Coach P&L.
- **Owner-authored intent (where it exists):** `positioning_intents` /
  `PositioningProfile` — the best-designed owner-context object in the repo: every field
  owner-expressed, nothing defaulted-and-hidden, append-and-supersede versioned, form
  re-validation so the LLM's numbers never persist.

What is thin, scattered, or dead:

| Gap | Evidence |
|---|---|
| **No canonical owner profile.** Household facts (income, tax buckets, baby 2031, work-break cadence, glide path) live in `../wealthplan/data/plan.local.json` — gitignored, unversioned, `.bak`-copy hygiene. The fullest persona (human-capital buckets + caps, philosophy, numeric bars) lives in `../portfolio-tracker/CIO_CONTEXT.local.md` — gitignored, "Last reviewed 2026-05-27," no history, manually synced to a DB table by documented convention only. earnings-summary itself has **no owner-profile object at all.** | Part-B store inventory |
| **Owner context in code constants.** `TaxProfile` default hardcoded in `position_tax.py` (`data/tax_profile.json` override doesn't exist on disk); the five `_behavioral_rules` are a frozen prompt string manually distilled from `seed.json` once — only rule 1's evidence count is live; rules 2–5 can silently drift from reality forever. | advisor inventory |
| **Captured-but-orphaned fields.** `PositioningProfile.sharpe_floor` and `target_vol_ann` are elicited from the owner, displayed, and consumed by **no computation** in Risk v2, Fit, what-if, or next-dollar. | fit/risk inventory |
| **Generic math wearing personalized clothes.** Risk v2 (factor loadings, crowding at hardcoded 0.70 corr, tail stress, thesis-collision) takes zero owner risk-appetite input. Next-dollar's `BLEND_WEIGHTS = 50/30/20` is an un-elicited house view; the model has no cash/liquidity input and reallocates only among current holdings. Base Fit is book-descriptive; only the Fit-v2 *target* factors reflect owner intent — and only when an intent row exists. | fit/risk inventory |
| **Starved circuits.** Worldview anchor live-ON in prod but **0 current Tenets → emits nothing into every prompt, indefinitely**. `position_sizing_intent` near-empty (FLKR ladder + two DRAFT add-rungs are the exceptions). `next_dollar` memos permanently `unscoreable`. Three outcome ledgers (`decisions`, `predictions`, `coach_pings`/`stance_scores`) never joined into one "did my calls work" view. | Ledger inventory |

**Therefore the tenet-2 strategy is not "build a coach." It is: (a) build the one missing
substrate — a canonical, versioned, consent-gated owner-context layer; (b) wire it into
the machinery that already runs; (c) light the circuits that are built but starved; (d)
close the measurement loop into a single decision journal. Almost no new surfaces.**

---

## 2. Inventory: owner-context usage per component (reference table)

Precise answer to "how much of the advisory machinery actually uses owner context":

| Component | Owner context actually consumed | Generic / hardcoded |
|---|---|---|
| `/review` PreAnalysis + guard | graded sell record (live query); stance/decision/musing notes; frozen `_behavioral_rules`; `TaxProfile` (hardcoded MFJ/CA/$450-500k); per-account `tax_treatment` from tracker | break-rule status, DCF ladder, weight-vs-band (band mostly `no_band` — intents empty), ≥8% concentration flag |
| Socratic flow | owner's typed answers ARE the memo; calibration block (own hit-rate); Worldview anchor (starved) | thesis/bear anchors |
| Next-dollar memo | open notes; sizing audit (mostly `unstated`) | book structure, DCF upside; memo itself never graded |
| Swap checks | open notes; tax placement | 15pp DCF-margin screen |
| Fit v2 target factors | `positioning_intents` (tilt, sector targets, sleeves) | fallback: target ≡ current book → fit_target ≡ fit |
| Fit base / what-if | **none** | hardcoded bands; pro-rata-only funding; weight menu fixed |
| Risk v2 (4 modules) | **none** | all thresholds hardcoded; `sharpe_floor`/`target_vol_ann` orphaned |
| Next-dollar model | **none** (no cash, no appetite, no targets) | `BLEND_WEIGHTS` 50/30/20 house view |
| risk_reward (L7) | `position_sizing_intent` conviction | gap thresholds hardcoded |
| Coach pings governor | freshness vs owner's open items; dismissal history | moment classes fixed at 3 |
| Ledger answer / reply | owner's own words; ContextPack anchors | no household/profile anchor slot exists |

---

## 3. Design (b): the owner-context layer

### 3.1 Principles

1. **Owner-authored or owner-ratified only.** Follow the two proven patterns:
   `PositioningProfile` (owner-expressed fields, never invented, form re-validated) and
   falsifier ratification ("(inferred)" facts are inert until ratified). A derived fact
   about the owner never conditions advice until the owner has affirmed it. This is the
   anti-creepiness mechanism: the profile contains nothing the owner didn't say or
   approve, and every prompt injection is spotlight-wrapped like the Worldview anchor.
2. **Derive, don't interrogate.** No questionnaire wizard. Seed from what already exists
   (wealthplan JSON, CIO_CONTEXT, seed.json, graded history) and present for
   ratification — the same pattern as tenet distillation and the seed-decision backfill.
3. **Versioned like intents, never overwritten.** Append-and-supersede with
   `is_latest` + supersede chain (the `positioning_intents` / `dcf_runs` precedent).
   Point-in-time reads must be possible: "what did the coach know when it said X" is a
   measurement requirement (§5).
4. **One canonical home; adapters, not copies.** earnings-summary becomes the canonical
   store for the *advisory-relevant* profile. Sibling repos keep their own operational
   files; a one-way import adapter (mirroring the existing `book_cma.json` export
   pattern, reversed) snapshots what advice needs. No bidirectional sync.
5. **Freshness is enforced, not hoped for.** Every profile fact carries
   `affirmed_at`; the coach's existing freshness gate extends to profile facts — a fact
   past its review horizon is quoted with its age or not used, never silently assumed
   current. (The owner's own rule: "if it becomes stale, I just won't even use it.")

### 3.2 The profile object: three tiers

New table `owner_profile_facts` (append-and-supersede; one row = one fact; category +
key + typed value + narrative + provenance + `affirmed_at` + review-horizon):

- **Tier A — capacity (imported, ratified).** Household facts advice actually needs,
  snapshotted from `wealthplan/data/plan.local.json` by a new
  `execution/import_owner_capacity.py`: tax-bucket balances (pretax/roth/hsa/taxable/
  cash/illiquid), income & savings-rate trajectory, cash buffer months, glide-path
  posture, dated life events (baby 2031, work breaks, "Quit Meta 2028"), horizon ages.
  Import is **manual/on-demand + a staleness reminder**, never a silent cron — the file
  is the owner's private plan; each import lands as `proposed` facts the owner ratifies
  (one packet-walk pass). Also fold in the human-capital correlation buckets + caps from
  CIO_CONTEXT (currently only in the sibling repo).
- **Tier B — appetite & policy (owner-authored).** Extends what `PositioningProfile`
  started: max drawdown tolerance, vol target (wire the orphaned fields), cash-floor
  rule, per-name max weight, dry-powder policy, sleeve targets. Where a fact is already
  representable in `positioning_intents`, it stays there — the profile table
  references, not duplicates, the active intent. `position_sizing_intent` ladders
  (FLKR pattern) remain the per-name expression.
- **Tier C — behavioral self-model (derived, ratified, LIVE).** Replace the frozen
  `_behavioral_rules` string with rows distilled from the graded `decisions` corpus on a
  cadence (same distill→propose→ratify machinery as Tenets; rules are re-derived when
  grading shifts the evidence). Rule 1 today would distill identically; the difference
  is that when the owner's next 10 graded exits stop confirming the pattern, the rule
  *weakens on its own* instead of nagging forever. The prompt block is composed from
  current rows at call time — `_behavioral_rules` becomes a renderer, not a constant.

### 3.3 Keeping it current without being creepy

- Ratification-only ingestion (no ambient collection; imports are owner-initiated).
- Review horizons per category (capacity: quarterly or on life event; appetite: on
  pledge >$10k or drawdown >15%; behavioral: after each grading batch). The **monthly
  red-team** gains one standing lens: "is the profile stale or contradicted by observed
  behavior?" — surfacing drift as a packet item, not auto-editing.
- The weekly packet is the affirmation surface: expiring facts appear as one-tap
  `[Still true / Update / Drop]` items. No new UI.

---

## 4. Design (c): delivery — interventions, not panels

Rule: **zero new consoles.** Every delivery rides an existing decision moment, reusing
the governor's interruption discipline. Five injection seams (all confirmed in code):

1. **`/review` + pledge challenge (the trade moments).** PreAnalysis gains a
   *capacity block*: this trim's proceeds vs. stated dry-powder policy; position vs.
   human-capital bucket cap (MELI+NU vs. the Brazil factor; META vs. `big_tech_ads`);
   horizon events within the holding's horizon ("baby-2031 liquidity window").
   Deterministic lines, same rendering path, tax-block precedent.
2. **Anchor slot (every governed prompt).** A 6th `owner_profile` anchor in
   `compose_anchor_block` — tight char cap, dated, spotlight-wrapped, "soft priors NOT
   rules," exactly the Worldview-anchor design. Socratic, ledger answers, coach pack,
   position review all inherit it for free.
3. **Governor moment classes (initiated coaching).** Add classes to the existing
   deterministic governor (no new caps machinery): `profile_drift` (behavior
   contradicts an affirmed fact), `capacity_breach` (human-capital cap or cash floor
   crossed — today these caps live only in the sibling tracker), `life_event_checkpoint`
   (dated event approaching). Same 1/day cap, freshness gate, auto-mute.
4. **Morning pipeline / inbox.** Advisory findings enter as alert rows scored by the
   existing `decisive_alert_reason` / `_strength_factor` seam (an owner-falsifier-breach
   analogue: "owner-policy breach"), not as a new feed.
5. **Weekly packet + packet walk.** Advisory items (expiring facts, drift findings,
   un-attested reviews past their window) are a 5th item source in `_packet_items` /
   `assemble()` — the collectors were built for exactly this.

Explicitly rejected: a "profile dashboard," a chat-first advisor persona (the coach-pack
chat already exists for positioning), and any always-on nag outside the governor.

---

## 5. Design (d): measurement — the decision journal

The success bar is already honestly instrumented (attestation-gated counter, source
tags, elapsed-window candidates). What's missing is the *unified* view and the
*improvement* signal:

1. **Decision journal = one read view over the three ledgers.** `v_decision_journal`
   joining `decisions` (owner + advisor), `advisor_memos`/`stance_scores`,
   `coach_pings`, `decision_nudges`, `predictions` — keyed by ticker + time, each row:
   what was decided, what advice existed *before* the decision (point-in-time profile
   version included), disposition (followed/ignored/overridden), outcome, process
   quality. No new writes; a view + one panel section in the existing allocation panel.
2. **Advice-influence read.** Quarterly (riding the existing calibration scorecard
   job): graded outcomes partitioned by "advice delivered before decision vs. not" and
   "followed vs. overridden," with the scorecard's min-n honesty floor — below floor it
   prints counts and says "too thin," never a verdict. This is deliberately *descriptive*
   at n≈dozens; the point is a habit of asking, not statistical significance.
3. **Close the grading holes:** make next-dollar memos scoreable (grade the top-ranked
   pick's relative return at horizon — mechanical, like swap checks); grade guard
   overrides (a `guard_override` hold IS a stance — score it at 90d like any hold, so
   the guard itself accrues a track record; today its wins are invisible).
4. **Instrumentation:** interventions log to the existing `panel_activation_counts`
   namespace (`act:advice:*`) plus their own ledgers (pings/nudges already do). No new
   event system.

---

## 6. Roadmap (e): phases, PR-per-phase, tenet-1 deconfliction

### Collision map (from the tenet-1 program docs)

Tenet-1 waves (metrics engine, segment quarterly, comparable sets) converge on:
`financial_facts`/`kpi_facts` write paths, `tracked_companies` columns,
`generic_xbrl_capture.py`, quarter-keying helpers, the LLM 4-registry +
`llm_quota_scheduling.md`, and (final phases only) `src/ui/controls.py` +
`tests/test_ui_controls.py`. **Tenet-2 touches almost none of these** — it lives in
`src/advisor/`, `src/positioning/`, `src/onmymind/`, `src/research/governor.py`,
`src/llm/anchors.py`, and its own new tables. Genuine shared surfaces, with the rule:

- **Alembic numbering** — pick migration numbers at rebase time (standing gotcha).
- **LLM 4-registry + quota windows** — Phase 3+ adds at most 2 purposes
  (`profile_distill`, `behavior_distill`); register in lockstep, keep new scheduled legs
  out of 03:00–05:00 PT.
- **UI-kit gate** — every rendered addition is kit-composed; run
  `tests/test_ui_controls.py` per frontend change (both programs already bound by it).
- **`decisions` schema** — tenet-2 reads it heavily but needs no columns; any future
  need coordinates with the calibration jobs.

### Phases (each = one PR wave, independently shippable, flags default OFF)

- **Phase 0 — light the dead machinery (no schema, days).** Seed 3–5 Tenets from the
  distill queue in one owner session (kills the starved-anchor no-op); create
  `data/tax_profile.json` from the real bracket; encode the 2–3 standing sizing-policy
  rows the adversarial review already ratified; wire `sharpe_floor`/`target_vol_ann`
  into the tail-stress/positioning renders (display→consume); verify `capture_poller`
  task is enabled (backlog notes it Disabled — every Telegram intervention depends on
  it). *Success: Worldview anchor non-empty; zero orphaned profile fields.*
- **Phase 1 — profile substrate.** `owner_profile_facts` migration + store
  (append-and-supersede) + `execution/import_owner_capacity.py` (wealthplan +
  CIO_CONTEXT one-way import → `proposed`) + ratification via existing packet-walk
  card type. No prompt injection yet.
- **Phase 2 — context injection.** `owner_profile` anchor slot (spotlighted, capped,
  dated); PreAnalysis capacity block (deterministic); next-dollar gains a cash/liquidity
  input + owner-confirmed blend weights (kill the silent 50/30/20).
- **Phase 3 — initiated interventions.** Governor moment classes (`profile_drift`,
  `capacity_breach`, `life_event_checkpoint`); human-capital caps evaluated in-repo
  from Tier-A facts; weekly-packet advisory item source; owner-policy-breach alert
  class through `decisive_alert_reason`.
- **Phase 4 — behavioral layer v2.** Behavior distill→ratify pipeline;
  `_behavioral_rules` becomes a renderer over current rows; red-team profile-drift lens.
- **Phase 5 — decision journal.** The unified view + panel section; advice-influence
  read in the calibration scorecard; next-dollar + guard-override grading.

Sequencing intent: Phases 0–1 can run **during** any tenet-1 wave (disjoint files).
Phases 2–3 prefer a quiet week on the anchor/prompt surfaces. Q3'26 bar: Phases 0–3 are
what plausibly move a real decision before the deadline; 4–5 harden the loop.

---

## 7. Decision list for the owner

1. **Wealthplan import boundary.** May earnings-summary snapshot household capacity
   facts from `wealthplan/data/plan.local.json` into `data/portfolio.db`?
   (a) full Tier-A import as specced; (b) derived summaries only (buffers, dates,
   bucket totals — no comp figures); (c) no import, manual entry. *Recommended: (b) —
   advice needs the shape of capacity, not the payroll detail.*
2. **Canonical persona home.** Does `owner_profile_facts` become canonical for
   advisory context, with `CIO_CONTEXT.local.md` remaining the tracker's own input
   (one-way import, no sync-back)? Alternative: keep the persona in the tracker and
   fetch over REST. *Recommended: canonical here; the tracker stays independent.*
3. **Behavioral rules go live-derived** (Phase 4) or stay frozen prose? Live means
   rules 2–5 can weaken/strengthen with graded evidence, after your ratification.
   *Recommended: live-derived — a frozen self-model is the staleness rule violated.*
4. **Governor budget for new moment classes.** Fold `profile_drift` /
   `capacity_breach` / `life_event_checkpoint` under the existing ≤1/day ≤3/week caps
   (advice competes with falsifier breaches for the daily slot), or grant a separate
   ≤1/week advisory lane? *Recommended: same caps — scarcity is what makes pings read.*
5. **Next-dollar blend weights.** Confirm 50/30/20 as your view, set your own, or make
   it profile-driven? Also: should next-dollar gain a cash-aware absolute mode?
6. **Freshness cadence.** Quarterly affirmation packets + event triggers as specced, or
   event-triggered only? (Quarterly adds ~4 packet items/quarter.)
7. **Sequencing.** Interleave Phases 0–1 into the current tenet-1 wave now, or hold
   tenet-2 for a dedicated wave after the metrics-engine Phase 1 lands?
8. **Phase 0 owner sessions.** Two ~20-min sittings are the critical path: the Tenet
   seeding pass and the sizing-policy encoding pass. Schedule them?

---

## 8. What this program does NOT do

No trade execution, no order routing, no external-facing advice surfaces, no regulated
personalized-advice framing, no multi-user profile machinery (single-owner invariant
stands), no new chat personas, no new consoles, no ambient data collection, and no
advice conditioned on facts the owner hasn't affirmed.

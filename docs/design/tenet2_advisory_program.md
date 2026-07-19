# Tenet-2 Advisory Program — Strategy & Phased Roadmap

**Status:** RATIFIED 2026-07-17 — all 8 decisions in §7 ruled by the owner same-day.
Phase 0 execution authorized (subagent-prepared, owner-ratified); combined roadmap in §6.
**Scope guard (restated, non-negotiable):** this platform never executes trades, and it
never becomes an advice product for anyone but its single owner — no external-facing
surface (shared link, published artifact, exported report) carries advice framing. For
the owner it is *maximally* personalized decision support: it structures, challenges,
and quantifies; the owner decides. This is a surface/audience guard, not a
personalization limit.

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

- Ambient learning always-on; **assertion** is affirmation-gated (§7.1). Capacity
  imports are owner-initiated snapshots, never silent crons.
- **Owner-authored sources are pre-affirmed (owner ruling 2026-07-19).** The §7.1
  gate exists for *inferences about the owner* — wealthplan's plan file and the
  tracker's CIO_CONTEXT are values the owner typed and vetted at the source, so the
  importer lands them `affirmed` with **no review horizon**; freshness comes from
  re-running the import (a changed source value supersedes as a fresh affirmed row),
  not from quarterly "still true?" walks. Ratify-in-the-Ledger applies only to
  machine-**derived** facts (e.g. the `--seed-appetite` blend seed).
- Review horizons for derived/owner-typed categories (appetite: on pledge >$10k or
  drawdown >15%; behavioral: after each grading batch). The **monthly red-team**
  gains one standing lens: "is the profile stale or contradicted by observed
  behavior?" — surfacing drift as a packet item, not auto-editing.
- The weekly packet is the affirmation surface: expiring facts appear as one-tap
  `[Still true / Update / Drop]` items. No new UI.

### 3.4 Owner-context federation (owner directive 2026-07-17, first-class pillar)

The owner-context layer is substantially **read from** the two sibling systems, not
rebuilt here. Verified state (read-only sweep, cross-checked this session): the
ES→wealthplan `book_cma.json` export has **zero consumers** in wealthplan's tree (dead
leg); wealthplan's `tracker.py` reads the tracker's SQLite **directly**, bypassing the
tracker's API (schema-drift + file-lock risk); the ES tracker client works but coerces
via `_f()` with known pct-vs-fraction and Decimal-string hazards; the
`PortfolioTrackerApiServer` logon task exists and is Ready (the "no persistent
process" audit claim is stale as of 2026-07-16 — the residual issue is cold-start
latency, already handled by the client's tiered-timeout + `probe_tracker` design).

**Authority map (canonical, one owner per fact class):**

| System | Authoritative for | Read by |
|---|---|---|
| portfolio-tracker | positions, lots, transactions, accounts + tax treatment, benchmark/return/risk math, realized gains / exit quality | ES (existing REST client), wealthplan (today: SQLite bypass → migrate to API or a versioned export) |
| wealthplan | household model: comp/bonus/equity ×2, ContributionPolicy, ExpensePlan + COL, RetirementSettings/glide, life events (baby/house/move/work-break/startup/parent-care/exits), CMAs | ES Tier-A import (derived summaries per decision 1) + a new **capacity reader** for near-term cash-need schedules |
| earnings-summary | theses, research, decisions/grading, advisory memos, owner profile (affirmed facts), Tenets | tracker CIO advisor (its own stack, per the 2026-06 governance decision); wealthplan via `book_cma.json` |

**Read contracts & hygiene:** each leg gets a typed, versioned, units-explicit
contract (Pydantic on both ends; pct-vs-fraction stated per field) instead of ad hoc
coercion — formalizing, not rewriting, the existing client. Degradation semantics stay
the proven ones: never-raises, `available=False` + reason, advice degrades to
"capacity unknown, as of <date>" rather than guessing. Loopback-only, no auth — any
cross-machine ambition requires auth first (out of scope).

**Repair items owned by this program:** (a) ES-side **wealthplan capacity reader**
(cash-need/expense/goal schedule) feeding the `/review` capacity block — Phase 2;
(b) contract formalization of the tracker client's analytics payloads ES already
fetches but doesn't join into `/review` (realized-gain/exit-quality) — Phase 2;
(c) Tier-A import reads wealthplan's **models**, not hand-copied values — Phase 1.
**Repair items owned by sibling repos (tracked, not built here):** wire wealthplan to
actually consume `book_cma.json` (the red-team program built that bridge deliberately;
the consumer was never wired), and migrate wealthplan's SQLite bypass onto the tracker
API — both are wealthplan-side follow-ups.

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
   position review all inherit it for free. Only `affirmed` facts ride the anchor —
   ambient learning feeds the distillers continuously; affirmation gates injection
   (§3.1 principle 1).
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
- **Phase 1 — profile substrate + federation spec.** `owner_profile_facts` migration +
  store (append-and-supersede) + `execution/import_owner_capacity.py` (reads
  wealthplan's Pydantic models directly — derived summaries only per decision 1 — plus
  CIO_CONTEXT; lands as `proposed`) + ratification via existing packet-walk card type.
  Ships the §3.4 federation authority map + contract doc as part of this PR. No prompt
  injection yet.
- **Phase 2 — context injection + federation readers.** `owner_profile` anchor slot
  (spotlighted, capped, dated); PreAnalysis capacity block (deterministic), fed by the
  new **wealthplan capacity reader** (near-term cash-need/expense schedule) and by the
  tracker realized-gain/exit-quality payloads ES already fetches but never joins into
  `/review`; typed units-explicit contract models replace `_f()` coercion on the
  fields advice consumes; next-dollar gains a cash-aware mode + **profile-driven blend
  weights** from the appetite tier (hardcoded 50/30/20 = labeled no-profile fallback).
- **Phase 3 — initiated interventions.** Governor moment classes (`profile_drift`,
  `capacity_breach`, `life_event_checkpoint`); human-capital caps evaluated in-repo
  from Tier-A facts; weekly-packet advisory item source; owner-policy-breach alert
  class through `decisive_alert_reason`.
- **Phase 4 — behavioral layer v2.** Behavior distill→ratify pipeline;
  `_behavioral_rules` becomes a renderer over current rows; red-team profile-drift lens.
- **Phase 5 — decision journal.** The unified view + panel section; advice-influence
  read in the calibration scorecard; next-dollar + guard-override grading.

### 6.4 Combined roadmap — tenet-1 × tenet-2 interleaved (decision 7)

Tenet-1 inputs: metrics engine (ME P1–P3), segment quarterly (SQ P1–P3), comparable
sets (CS P1–P3) — phase definitions in their respective design docs (worktree
`ledger-ui-overhaul-58e513/docs/design/`). Tenet-2 phases (T2 P0–P5) per §6 above.
One PR per phase per program; waves are parallel worktrees; alembic numbers picked at
rebase time; LLM-purpose registrations serialized within a wave.

| Wave | Tenet-1 track | Tenet-2 track | Shared-surface watch |
|---|---|---|---|
| **A (now)** | ME P1 (engine skeleton + parity harness, no UI) | **T2 P0** (light dead machinery — subagent-prepped, owner ratifies) + **T2 P1** (profile substrate + wealthplan-model import + §3.4 federation spec) | none — fully disjoint files; alembic numbering only |
| **B** | ME P2 (full catalog + IFRS) ∥ SQ P1 (10-K-regime extraction; registers `segment_10q_period_disambiguate`) | **T2 P2** (anchor slot, capacity block + federation readers/typed contracts, profile-driven next-dollar) | 4-registry: SQ's new purpose lands this wave; T2 P2 adds none — no contention |
| **C** | SQ P2 (Q4 derivation + coverage) ∥ CS P1 (foundation, portfolio names) | **T2 P3** (governor moment classes, packet advisory items, policy-breach alerts) | quota windows: CS/SQ cron registrations + T2's governed pings all register in `llm_quota_scheduling.md`; keep 03:00–05:00 PT clear |
| **D** | CS P2 (widen + drift check) ∥ ME P3 (valuation metrics — **first tenet-1 UI**) | **T2 P4** (behavioral live-derive; registers `behavior_distill` ± `profile_distill`) | 4-registry again (T2's turn); UI-kit gate starts binding tenet-1 — run `tests/test_ui_controls.py` per PR |
| **E** | SQ P3 (FPI/MJDS, gated on spike) ∥ CS P3 (benchmark ratification + UI) | **T2 P5** (decision journal view + panel section + advice-influence read) | heaviest UI-kit convergence: three programs render in this wave — serialize the panel PRs, goldens regen per renderer touch |

Q3'26 bar: T2 P0–P3 (waves A–C) are what plausibly move a real decision before the
deadline; waves D–E harden the loop. If a wave slips, tenet-2 phases hold their wave
assignment rather than leapfrogging — the collision-avoidance depends on the pairing.

---

## 7. Decision list — RESOLVED 2026-07-17 (owner rulings in bold)

1. **Wealthplan import boundary.** **(b) — derived summaries only** (buffers, dates,
   bucket totals; no comp figures enter `portfolio.db`).
2. **Canonical persona home.** **earnings-summary's `owner_profile_facts` is
   canonical** for advisory context; `CIO_CONTEXT.local.md` remains the tracker's own
   input, one-way import, no sync-back.
3. **Behavioral rules.** **Live-derived** (Phase 4): rules re-distill from graded
   evidence, ratification-gated.
4. **Governor budget.** **Same caps** — advisory moment classes compete under the
   existing ≤1/day ≤3/week discipline.
5. **Next-dollar blend.** **Profile-driven** weights from the appetite tier, **plus a
   cash-aware mode**. Hardcoded 50/30/20 becomes the labeled no-profile fallback only.
6. **Freshness cadence.** **Both** — quarterly affirmation packets AND event triggers
   (pledge >$10k, drawdown >15%, life-event dates, grading batches).
7. **Sequencing.** **Interweave with tenet-1; maintain the combined roadmap in §6.4.**
8. **Phase 0 sittings.** **Subagent-prepared**: agents draft the Tenet seeds, the
   sizing-policy rows, the tax-profile file, and the orphaned-field wiring; the owner's
   sittings reduce to short ratification passes (packet walk).

### 7.1 Clarified posture (owner pushback, 2026-07-17)

Two §8 phrasings in the original draft were too broad and are corrected:

- **"No regulated personalized advice"** is a *surface/audience* guard: no external-
  facing surface ever carries advice framing, and the tool never advises anyone but its
  owner. It is NOT a personalization limit — advice tailored to the owner's capacity,
  circumstances, and psychology is the program's purpose.
- **"No ambient data collection" is replaced by "ambient learning, gated assertion."**
  The platform continuously learns from everything the owner produces — chats, musings,
  journal entries, nudge annotations, dismissals, graded decisions. What it may NOT do
  is treat an *inference about the owner* as true without affirmation: derived facts
  land as `proposed`, and only owner-affirmed facts condition advice or get quoted back
  ("you said X"). Learning is always-on; asserting requires a tap.
- **Refinement (owner ruling 2026-07-19): the gate is scoped to inferences, not to
  owner-authored data.** Facts snapshotted verbatim from files the owner writes and
  maintains (wealthplan's plan, the tracker's CIO_CONTEXT) were already asserted by
  the owner at the source — "if I entered it there, I looked at it; if I update it
  there, I looked at it too." The importer lands those `affirmed` with no review
  horizon; the packet-walk ratification applies only to machine-derived facts.

---

## 8. What this program does NOT do

No trade execution, no order routing, no advice on any external-facing surface, no
advising anyone but the owner, no multi-user profile machinery (single-owner invariant
stands), no new chat personas, no new consoles, and no advice conditioned on
*inferences about the owner* the owner hasn't affirmed (ambient learning is always-on;
assertion is affirmation-gated — see §7.1).

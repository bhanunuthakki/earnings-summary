# Directive: Close the Loops — next program (2026-06-13 re-score)

Canonical plan from the 2026-06-13 goal-anchored re-score (20-agent workflow: 8 pillars ×
score→adversarial-verify, + 3 synthesis lenses + a coherence critic). Run AFTER the
fund-grade build and the Interaction Paradigm program (both complete). Read this before
starting any session below. Models per `GEMINI.md` → Session & Agent Model Selection.

---

## 1. Pillar scorecard (graded against the hedge-fund-manager goal)

| Pillar | Grade | One-line |
|---|---|---|
| Frontend / UX daily driver | 7.0 | Fast, coherent, accessible shell — but the embedded **workspace report isn't responsive**, tablist keyboard-nav is declared-not-implemented, no offline-retry, dark-only. |
| Provenance / citation / credibility | 7.0 | Confidence + chips + lineage shipped — but confidence is an **uncalibrated formula**, it **never reaches the LLM's reasoning**, static LLM prose is unciteable, restatements are silent. |
| LLM advisory (multi-turn, grounded, eval-looped) | 7.0 | Packs + agentic loop + per-claim cites + evals — but the **answer's substance is never judged**, no cross-session memory, the advisor **never initiates**, can't reach the whole book / tax lots / scenarios. |
| Process capture / journal loop | 7.0 | Conditions + lifecycle + calibration shipped — but **nothing mines patterns across decisions**, pass/exit captured thinner than entry, qualitative conditions never resurface. |
| Modifiable DCFs | 6.5 | Scenarios + provenance + archetypes — but **modify→recompute is a Sheets/Excel round-trip** (the lone CRITICAL gap), price-leg freshness lies, sensitivity grid trapped in xlsx. |
| **Performance vs index / risk** | **5.5** | Weakest + lightest-touched: **no drawdown, no factor exposure** (betas discarded), whole-book stress is **CLI-only**, no risk-vs-reward asymmetry, blanks when the tracker is offline. |
| Data pipeline / ops / cost | 7.0 | Cron-health + backups + caches — but cron liveness isn't surfaced, silent-staleness is SEC-only, no spend-anomaly detection, single-machine fragility. |
| **The compounding loop (meta-goal)** | **5.5** | Weakest tie: calibration is **all-time only** (no "am I getting better?"), it **never flows back to the advisor** (open loop), no recurring-mistake naming, no skill decomposition. |

## 2. The thesis: the substrate is built, but the loops are open

Two programs turned this into a richly **instrumented** book — every fact sourced, every
stat a doorway, every decision logged, every DCF computed. The verified gap is now uniform
and structural: **the platform MEASURES but does not yet REASON ACROSS, INITIATE, or
INVERT.** Pure engines sit one seam from being decision spines — `marginal_risk` is never
divided into DCF reward; `value()/sensitivity_grid()` is pure but reachable only via xlsx;
`build_calibration()` computes real stats rendered to an inert panel, never injected into
the advisor at decision time; `supersedes_id` ground-truth is collected but never joined back
to score confidence itself. **This program is surface-and-feed-back, not build-from-scratch:
near-zero new tables; it closes circuits.**

## 3. The program — sequenced waves (de-duplicated across the 3 synthesis lenses)

The three lenses heavily overlapped; the critic de-duped them. Build each item ONCE.

### Wave A — near-free injection seams (parallel; ship first)
| # | Session | Model | Deps | Scope |
|---|---|---|---|---|
| L1 | **Self-calibration pack → advisor/socratic** (THE KEYSTONE) | Sonnet | — | Add a `calibration` PackSpec to `src/ask/packs.py` loading `decision_calibration.build_calibration` (overall + by-conviction hit-rate, reversal win/loss, time-to-outcome); when a focus ticker has a fresh conviction/pending decision, the matching cohort's hit-rate. Reference it in `advisor/context.py` + `socratic.py` prompts ("challenge this conviction against the analyst's documented calibration: high-conviction calls graded 40%, n=12"). **Add a minimum-n guard** ("n=3, low confidence"). Pure reuse, zero schema. Flips "measures me" → "confronts me at the decision moment." |
| L2 | **Provenance into reasoning** | Sonnet | — | In `ask/grounding.py` extend `_fact_item` (~599-610) so the model-read text carries the scored confidence + any cross-source ⚠ disagreement ("[conf 79%; SEC says $101M, 0.99% delta]" — both already computed, today stored only on the UI-popover dict); ensure `build_evidence_block` (~1085) emits it; one prompt rule to flag/discount low-confidence/disagreeing figures. Near-free; makes every grounded answer provenance-AWARE, not just provenance-decorated. |
| L3 | **Answer-quality eval rung + pin `ask_answer`** | Sonnet | — (INDEPENDENT) | Mode-B rubric `evals/rubrics/ask_advisory_answer.md` (grounding-correctness, risk/reward balance, calibration-vs-evidence, follow-up usefulness) + a loader replaying real `ask_turns` through `eval_judge`; give the conversational answer its own `ask_answer` purpose with an `LLM_MODELS` pin so the most expensive call (pass-1 rides the bare CLI default today) enters the downgrade/Gemini loop. **Prerequisite for every LLM-composed coaching/standup bet — land it in Wave A.** |

### Wave B — pure-engine reuse; close the two weakest pillars (parallel; no dep on A)
| # | Session | Model | Deps | Scope |
|---|---|---|---|---|
| L4 | **In-app DCF modify→recompute loop** (resolves the lone CRITICAL gap) | Sonnet | — | `RedesignInputs.from_dict` (today only `read_inputs(workbook)`) + POST `/api/dcf/recompute` running the existing pure `value()/apply_scenario()/sensitivity_grid()` (no xlsx) → fair value + bull/base/bear + grid as JSON; make the valuation card's assumptions editable controls that live-update the scenario block + an in-app sensitivity heatmap. Keep Push-to-Sheets as the explicit "commit". Preserve the immutable Opus-baseline + override ledger so edits are auditable. |
| L5 | **Whole-book risk cockpit** (closes 3 high-sev perf gaps in one panel) | Sonnet | — | Surface the existing `portfolio_macro_stress` lens (CLI-only today) in a Portfolio→Risk panel + a `/actions/run-scenario` SSE route; add book-level drawdown (max DD + underwater curve + recovery) from the tracker's daily TWR series; **stop discarding the per-ticker beta/correlation rows** (`portfolio_tracker_client.py:408-419`) → a factor/style exposure rollup; persist a last-known risk snapshot to `portfolio.db` so the surface degrades to cached (stamped) values instead of blank when :8000 is offline. (Single cockpit session, may split to 2 PRs — NOT the 3-way split.) |
| L6 | **DCF price-leg freshness** | Sonnet | — | `over_under` feeds the trim/sell ladder + the 50%-weight next-dollar "ret" factor but goes stale on the price leg between brief rebuilds (refresh_dcf is opt-in, no cron). Re-divide the persisted fair value by live price on the snapshot read path OR a cheap daily re-price cron; mark coverage-panel freshness honestly on the price leg. Pure arithmetic over persisted fair value. |
| L14 | **Ask conversational latency / prompt caching** (owner pulled IN, 2026-06-13) | **Opus** (caching architecture) then Sonnet | — | The transport is a cold `claude -p` that re-encodes the whole thread + evidence every turn with no caching; the Wave-C Opus bets ADD calls on top, so this lands **before Wave C**. Design + build turn-level caching of the stable system/evidence prefix across turns within a thread (respecting the subscription-billing `claude_cli` path — verify what caching the CLI envelope supports; if native prompt-caching is unavailable on `-p`, fall back to evidence-block reuse / dedup + skipping re-retrieval when the pack set is unchanged). Measure turn latency before/after. Opus for the caching-architecture judgment against the CLI constraints. |
| L15 | **Tax-lot Ask pack** (owner: tax lots now, options later) | Sonnet | — | Surface the tax-lot + tax-treatment data that already exists in `integrations/portfolio_tracker_client.py` into the Ask holdings pack so "which lot do I sell to harvest a loss / minimize tax" is answerable. Tracker may be offline — degrade gracefully (reuse the L5 cached-snapshot pattern if landed). **Options/derivatives deferred** — the position model has no representation for them (a separate, larger effort). |

### Wave C — Opus judgment bets (each gated behind its eval / dep)
| # | Session | Model | Deps | Scope |
|---|---|---|---|---|
| L7 | **Risk-budget allocator** (the perf frontier) | **Opus** | L4 | Join `allocation/covariance.marginal_risk` (per-name risk contribution) × DCF bull/base/bear reward asymmetry × recorded conviction → each position's share of book RISK vs share of expected REWARD vs conviction; flag mismatches ("NU is 22% of book risk but 9% of expected reward, rated 3/5") as a single risk-parity-gap ranking. Also fix the asymmetry leak: the sizing audit + next-dollar model read only base `fv_gap`, ignoring the bull/bear FVs already in `dcf_runs`. Judgment-heavy modeling. |
| L8 | **Calibration coach + pre-mortem** (the compounding frontier) | **Opus** | L1, shared attribution engine | (a) Period-over-period cohorts in `build_calibration` (GROUP BY made_at quarter — today one flat pass) → "am I getting better?" as a curve; (b) the **shared selection/sizing/timing attribution engine** (see §6 — build ONCE, also serves L5's Brinson view); (c) LLM-synthesize 2-3 NAMED recurring biases each evidenced by linked decisions; (d) pre-mortem-from-your-own-history that auto-drafts candidate `decision_conditions` calibrated to documented blind spots; (e) one falsifiable behavioral experiment/period written back as a tracked decision_condition. Eval-gated (needs L3). |
| L9 | **Proactive analyst standup** (the advisory frontier — "run it like a PM") | **Opus** | L1, L3 | A scheduled job watches falsifiable `decision_conditions` + journal open-items + DCF staleness + position drift; on a trip, composes a grounded CITED message into a persistent `ask/store.py` session ("NU crossed the NPL threshold you said would change your mind [n] — here's the evidence; re-open the thesis?"). Bridge **qualitative** conditions to the existing material_news/earnings_tone triggers (today extraction skips no-number triggers). Eval-gated + rate-limited (a chatty/miscalibrated advisor is worse than silence). |
| L10 | **Self-calibrating credibility engine + restatement push** | **Opus** | L2 | Join `supersedes_id` restatement chains (collected but discarded at every call site) + resolved source-disagreements back against stored confidence → a Brier/reliability table per confidence bucket / source tier on the Provenance console; **gate replacing the hand-set tier constants on a minimum-n**, falling back to constants until then. + fire a **Restatement trigger** (none in the registry today — supersedes are silent) on a material held-name supersede. |

### Wave D — input side + cleanup
| # | Session | Model | Deps | Scope |
|---|---|---|---|---|
| L11 | **Capture errors of omission** (first-class pass/avoid + qualitative resurfacing) | **Opus** | — | An authoring path for "I passed on X because Y" / "avoiding sector Z" as a first-class `decisions` row (`recommendation_kind='avoid'`) with falsifiable conditions, wired from the discovery "dismissed" action (today writes only queue-state) so a passed name that later triples leaves a graded trace; bridge qualitative conditions to news/tone triggers so "CEO leaves"/"competitor enters" fires on news, not next quarter's XBRL. Feeds richer material into the loop L1/L8 close over. |
| L12 | **Static-prose citations** | Sonnet | L2 | Extend `ui/prose.py::render_prose` to optionally linkify `[n]` marks against a citations payload; wire the static LLM-prose renderers (thesis, bear, synthesis lenses incl. mgmt_credibility, attribution narratives) to produce + pass it — so the paragraphs interpreting the facts are as traceable as the fact cells. |
| L13 | **Frontend mechanical fixes** | Haiku | — | (a) Responsive embedded workspace report — **verify first** whether `@media` lives in `workspace_html.py` (scorer found none) or `workspace_styles.py` (a synth claimed some exist); collapse multi-column grids + `display:block;overflow-x:auto` tables on narrow widths. (b) Roving-tabindex + arrow-key nav on the role=tablist nav/sub-tabs (ARIA contract declared, JS absent). (c) Offline/online banner + one-click retry on tracker-fed panels. (d) Wire the already-built light/paper token mode to a theme toggle. All spec'd, no judgment. |

## 4. Coverage gaps / open decisions — RESOLVED (owner, 2026-06-13)

1. **Ask conversational latency / prompt caching** — **IN this program → L14** (Wave B, Opus then Sonnet), lands before Wave C.
2. **Conviction-as-probability (Brier)** — **DEFERRED.** "Am I getting better" is answered with hit-rate trends (L8, cheap, no schema); numeric Brier calibration (schema + UX to log a predicted probability per decision) is a future call, not this program.
3. **Cross-session advisory memory** — unchanged: stub in L9; a real per-ticker conclusion store is a follow-on if the stub proves insufficient (mind the hazard: naive memory re-asserts a conclusion the current-state packs have since invalidated).
4. **Tax-lot / options reasoning in Ask** — **tax lots IN now → L15** (Wave B, Sonnet, data already in the tracker client). **Options/derivatives deferred** (no position-model support).
5. **Agentic recompute-from-chat** — unchanged: a one-seam follow-on after L4 (wire `/api/dcf/recompute` as an Ask tool); more attractive once L14's caching lands. Fold into L9 or a micro-session later.

## 4b. Status (2026-06-13)

- **Wave A SPAWNED** — L1 (self-calibration pack, keystone), L2 (provenance into prompt), L3 (answer-quality eval); all Sonnet, dependency-free, parallel. Reconciled against `residual_backlog_2026_06.md` (no overlap).
- Owner decisions folded above; **L14 (latency) + L15 (tax lots) added to Wave B.**
- Wave B/C/D unspawned; spawn after Wave A merges. L3 must land before L8/L9; L14 before Wave C; L6 before/with L7.

## 5. Highest-leverage first moves (the critic's ranking)

1. **L1** — self-calibration pack → advisor. The data is computed and sitting inert; a PackSpec + a socratic prompt reference is pure reuse, zero schema, and is the only move that converts the goal's keystone ("smarter over time") from a static scorecard into a live feedback loop. Cheapest path to the highest-priority goal aspect.
2. **L2** — provenance into the prompt. Confidence is computed but the answer-writing model never sees it. Highest leverage-per-line in the backlog.
3. **L4** — in-app DCF recompute. Resolves the lone CRITICAL gap and unblocks L7's reward leg.
4. **L5** — whole-book risk cockpit. Turns the lightest-touched, co-lowest pillar from absent to real, mostly by surfacing substrate that already exists.
5. **L3** — answer-quality eval rung. Makes "eval-looped" true for the advice itself, and is a hard prerequisite for trusting the Opus coaching/standup bets, so its leverage compounds.

## 6. Coordination

- **ONE shared attribution engine.** L8's selection/sizing/timing skill decomposition and L5's Brinson allocation-vs-selection view are the same decomposition from two pillars — build it once (`src/attribution.py`), consume from both. Do not ship two divergent alpha decompositions.
- **L3 (answer-quality eval) lands before L8/L9 ship** — an LLM-composed coach/standup that misreads the owner's history is worse than silence. Soft dependency; honor it.
- **Minimum-n guard is shared** (L1 + L8 + L10): never confront the owner / replace a constant from a sparse denominator; frame "n=3, low confidence" honestly.
- **Verify the workspace-responsive discrepancy** (L13) before sweeping — the scorer and a synth disagreed on whether `@media` rules already exist and in which file.
- **DCF freshness couples to the allocator.** L7's reward leg inherits any staleness in fair value — L6 (price-leg freshness) should land before or with L7 so the risk-parity-gap signal isn't computed on stale reward.
- One PR per phase, alembic numbers (few here — most sessions are reuse-only) picked at REBASE on the live head; the standard parallel-wave hygiene applies.

## 7. Correction to a scorer claim

The compounding lens verified that **decision grading IS wired** — `execution/grade_decisions.py` calls `record_outcome` via a registered weekly cron, so `outcome_label` is populated and the calibration denominator is NOT empty (the per-pillar scorer wrongly flagged grading as unwired). The loop's break is the **return path** (calibration never reaching the advisor), not the grader.

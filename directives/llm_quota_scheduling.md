# LLM quota & scheduling discipline (agent bursts × cron fleet)

**Why this exists.** The app's in-app LLM transport is Codex-membership first
(`src/llm/cli.py` → isolated `codex` CLI), with the subscription `claude` CLI
as its first operational fallback. Each pool is finite and shares quota with
its corresponding interactive sessions on this machine. On 2026-07-02 and
2026-07-03 two consecutive all-night
multi-agent build waves exhausted the window, and the 04:00 morning pipeline's own LLM
calls failed with `claude.CMD` exit 1 two mornings running — stage 0b died whole (pre-#814)
and the failure looked like a code bug, not a quota artifact. The owner's directive
(2026-07-03): *"shift/segment bursts to every 6-7 hours."*

The global rule lives in the machine rulebook (`~/.gemini/AGENTS.md` → "Scheduling &
Quota Discipline"); this directive is the repo-specific instance and the **window
registry**.

## Rules

1. **Segment bursts.** Multi-agent / parallel-session waves run as bounded bursts spaced
   **≥6–7h apart**, each sized to finish inside the current session window. One wave per
   window; never chain waves back-to-back overnight.
2. **Protect the fleet's LLM windows** (registry below). No wave still burning through
   **03:00–05:00 America/Los_Angeles**. When registering a NEW scheduled job with an LLM
   leg, pick a slot clear of the registry and of likely burst hours, then add it to the
   registry table in this file (same PR).
3. **Per-item degrade is mandatory** for every scheduled job with an LLM leg: a transient
   CLI failure (frequently = exhausted quota; surfaces as `CalledProcessError` exit 1, or
   the fallback layer's `RuntimeError` when `LLM_FALLBACK_DISABLED=1`) defers *that item*,
   logs an explicit `deferred_transient` tally, and retries on the job's next run. Hard
   stops (`LLMSetupError`, hard-block budgets — `llm.cli.is_hard_stop`) still propagate
   loudly. Reference implementation: `attach_conditions` /
   `attach_qualitative_conditions` in `src/decision_conditions.py` (#814) +
   `execution/record_decisions.py`'s tally line.
4. **Resume, don't respawn.** An agent killed by the quota limit ("session limit resets
   <hour>") keeps its context — message it to resume after the reset; don't redo the work.

## Window registry — scheduled jobs with LLM legs (America/Los_Angeles)

| Task | Schedule | LLM legs | Notes |
|---|---|---|---|
| `run_morning_pipeline` | daily 04:00 | stage 0 news → `material_news_classification`; stage 0b `decision_conditions_extract` (+ qualitative twin); stage 1b standup (`run_standup.py`) → `ask_answer` compose + `eval_judge` gate per surviving trip; stage 1c `pre_earnings_brief` (Sonnet, one call per name reporting within 7d — portfolio + `auto_pre_earnings_brief`-marked evaluation names; llm_artifacts input-hash cache + T-1 refresh gate bound each name to ≤2 calls per earnings cycle, so a normal morning is 0-2 calls; budget skip-mode $5/mo, 0260; per-item degrade — transient defers the ticker, tallied `deferred_transient`, retried next morning; hard stops exit 2; added 2026-07-31, owner ruling relaxing D2 for this one artifact) | the canonical protected window; post-#814 degrades per-item; stage 1b was the previously-undocumented leg behind the 2026-07-13 incident (below) |
| `refresh_scenario_priors` | monthly, 1st 03:00 | `scenario_prior` (Sonnet) | `--only-changed` + `inputs_sha256` → usually zero calls |
| weekly eval rungs — SPLIT across two windows (registry verified live 2026-07-25): `run_grade_calibration` **Sun 10:30** (task `grade_calibration`); `run_weekly_model_eval` **Sat 20:00** (task `model_eval_sweep` — this table previously lumped both at "Sun 10:30", registry/reality drift of the same class as the 03:30 incident below) | Sun 10:30: `eval_judge`, rubric audits; Sat 20:00: sweep candidates + judging, **+ prompt A/B cycles** (2026-07-25, meta_eval_governance.md §4.7: `run_weekly_model_eval` step 4 — `prompt_variant_propose` (Opus) + `scope='prompt_ab'` arm runs/judging, default 2 cycles/run, governed by the $40/mo month-to-date measurement-spend ceiling checked BEFORE each cycle, not by cycle count; rides the Sat 20:00 window, no new registration); **+ `behavior_distill`** (tenet-2 Phase 4, one batch call over the graded `decisions` corpus, added as the LAST rung 2026-07-18) | daytime slot — deliberately outside burst hours; the LIVE Windows Task Scheduler entry had drifted to 03:30 (inside the protected window, colliding with `run_morning_pipeline`) — re-registered to 10:30 on 2026-07-13, and `cron/grade_calibration.task.xml` corrected to match so a re-register can't revert it (see incident note below); `behavior_distill` rides this SAME window (no new cron entry) and follows rule 3 (transient failure defers + tallies, retried next Sunday; a hard stop propagates as a non-zero exit, counted like any other rung failure by `execution/run_calibration_grading.py` — see `synthesis.behavior_distill`) |
| `ledger_synthesis` | daily (morning block) | `theme_synth` | cost-capped; degrades to "synthesis not available" |
| capture poller (service, event-driven) | continuous | `capture_intent` (Haiku per musing), `artifact_brief` (Sonnet), `capture_triage` (Haiku per landed musing/observation — replaces the regex answer gate), `coach_reply_intent` (Haiku per free-text reply to a coach ping, B3), `research_triage` (Haiku, B7 — one extra call per `wondering` verdict routing answer_now/belief_candidate/research_task; fails open to research_task on any failure, budget warn-mode 0194) | no fixed window; starved calls surface as missed classifications — budgets seeded warn-mode (0138/0139/0188/0194); `coach_reply_intent` volume is bounded by the governor's own DAILY_CAP=1/WEEKLY_CAP=3 (research/governor.py), so it's the cheapest leg on this row |
| `coach_pings` | daily 07:15 | **none** (zero-LLM governor) | listed to show it's quota-safe |
| `run_red_team` | monthly, first Saturday 10:00 | `red_team_attack` (per held name, rotating lens), `red_team_cross_book` (factor-block / style-drift / human-capital, 3 calls) | daytime weekend slot, clear of the fleet's protected windows; ~15 calls/run; per-item degrade (directives/monthly_red_team.md Phase 2); Windows Task Scheduler has no native Nth-weekday trigger, so the task fires every Saturday and the script itself no-ops unless it's the month's first (`execution/run_red_team.py::is_first_saturday`) |
| `run_weekly_packet` | Sunday 08:00 | `weekly_packet_predraft` (Haiku, per packet item) | navigation_ia.md §3.1 (PR2) — daytime weekend slot, clear of the fleet's protected windows; per-item degrade (a failed/unparseable predraft ships the item without a suggested verdict, never blocks the packet); task registered: `cron/weekly_packet.task.xml` → `\earnings-summary\weekly_packet` (2026-07-13); B7 (2026-07-19) added a DETERMINISTIC "expiring research" section to the same send (`pipeline.weekly_packet._send_expiring_research`) — no new LLM leg, it is a plain `research_tasks` query + per-item degrade on the Telegram send only |
| `run_decision_nudge` | daily 17:00 | **none** (zero-LLM — the scan and message text are both deterministic) | quota-safe at any hour, like `coach_pings`; evening slot because the owner answers Telegram in the evening; at-most-once-per-decision (UNIQUE ledger) so the daily sweep never re-pesters; task registered: `cron/decision_nudge.task.xml` → `\earnings-summary\decision_nudge` (2026-07-13) |
| `fetch_news` follow-on: diet quality scoring | with every news fetch (stage 0 of `run_morning_pipeline`, daily 04:00; also ad-hoc fetches) | `diet_source_quality` (Haiku, batched: ≤20 rows/call, ≤5 calls/run cap — since the 2026-07-30 narrowing, typical volume is ~1–2 calls/run at ~20–25 renderable rows/day; the cap is now a ceiling, not the steady state) | scores unscored RENDERABLE diet signals 0..1 at ingest (`src/signals/quality.py`) so `load_diet_signals` filters on the stored score instead of the static publisher denylist. Scope narrowed 2026-07-30 (diet headline-news removal follow-up): only `consensus_rating` + EDGAR-fed `general_news` are scored — the score drops no-new-info sell-side reiterations and 13F position-tweak trivia; non-EDGAR `general_news` (yf_news/websearch/fmp) no longer renders on the diet, stays NULL, and the read-time denylist fallback governs it. Per-item degrade unchanged (a transient CLI failure / twice-unparseable batch leaves those rows NULL → denylist fallback at read time, retried next fetch; hard stops propagate); no new cron — rides the existing protected window (2026-07-16) |
| `thesis_collision` | Saturday 11:00 | `thesis_collision` (one whole-book clustering call) | program review 2026-07-19 phase A4 — the engine/validation/Risk-tab reader shipped long before but was never scheduled (0 runs ever); cached on the thesis-set hash so an unchanged book re-runs at zero spend; slot clear of the 03:00–05:00 window, the Sat 10:00 red-team and the Sunday rungs; task: `cron/thesis_collision.task.xml` → `\earnings-summary\thesis_collision` (2026-07-19) |
| `session_distill` | daily 18:00 | `session_distill` (Sonnet, one structured call per undistilled session — idle≥4h Ask threads + landed bridged transcripts); **+ `exit_postmortem_draft`** (Sonnet, one structured call per pending closed-position post-mortem — ≤3/run nightly pacing, or ALL pending in one batch under `--postmortem-backfill`, added as a SECOND leg on this same rung, B6, 2026-07-19 program overhaul) | B4 keystone (2026-07-19 program overhaul) — evening slot, clear of the 03:00–05:00 window and every other registered rung; per-item degrade (a transient CLI failure defers that session, tallied `deferred_transient`, retried next sweep — quota rule 3; a hard stop propagates, exit 2); budget warn-mode (0190, session_distill cap $15/mo; 0193, exit_postmortem_draft cap $5/mo); task: `cron/session_distill.task.xml` → `\earnings-summary\session_distill` |
| `tenet_accountability` | Saturday 09:00 | `tenet_accountability` (Sonnet, one structured call per CURRENT Tenet WITH owner decisions since its `as_of` — a Tenet nobody has acted on costs zero LLM calls; ≤~15 calls/run at current Tenet-count scale) | B5 (2026-07-19 program overhaul) — weekend morning slot, clear of the 03:00–05:00 window, the Sat 10:00 red-team slot and the Sat 11:00 `thesis_collision` slot, and the Sunday eval rungs; per-item degrade (a transient CLI failure defers that Tenet, tallied `deferred_transient`, retried next week's sweep — quota rule 3; a hard stop propagates, exit 2); the governor's own `tenet_challenge` moment class reads the persisted verdict and makes NO LLM call of its own; budget warn-mode (0192, tenet_accountability cap $10/mo); task: `cron/tenet_accountability.task.xml` → `\earnings-summary\tenet_accountability` |
| `refresh_business_factors` | Sunday 11:30 | `business_factor_taxonomy` (Sonnet, one structured call per portfolio holding — ~11 calls/run at current book size) | C3 (2026-07-19 program overhaul Workstream C keystone, `src/risk_factors.py`) — weekend late-morning slot, clear of the 03:00–05:00 window, the Sun 08:00 `weekly_packet` send, the Sun 10:30 eval rungs, and the Sun 14:00 myclaw `weekly_review`; cached on a per-ticker mix+thesis input hash so an unchanged holding re-runs at zero spend; per-item degrade (a transient CLI failure defers that ticker, tallied `deferred_transient`, retried next week's sweep — quota rule 3; a hard stop propagates, exit 2); task: `cron/refresh_business_factors.task.xml` → `\earnings-summary\refresh_business_factors` (registered 2026-07-24, first live run TBD outside the 03:00–05:00 window) |
| `compose_senior_partner_brief` | Sunday 09:00 (weekly synthesis) | `senior_partner_brief` (one governed structured call per week) | **SHIPPED + REGISTERED** (#1002, 2026-07-24; two supervised no-Telegram runs verified before live delivery). Slot clear of the 03:00–05:00 window, the Sun 08:00 `weekly_packet` send, and the Sun 10:30 eval rungs. Composes over `weekly_packet`, the latest valid Risk Budget, the latest Incremental Dollar Recommendation, Investment Decision Cards, Worldview, and decision/calibration history. Budget: `on_exceed='block'`; immediate/exceptional triggers independently follow rule 3. |
| `disclosure_change_sweep` | Saturday 14:00 | Existing P0→P3 disclosure detector purposes + `disclosure_thesis_materiality` (Sonnet elevation gate, LAST step — batched ≤40 events/call, ≤150 events/ticker/run backlog drain; no-thesis tickers and tabular rows skip at zero spend; added 2026-08-02, #1134), only for tracked tickers with a new accession since the last sweep | New-accession fast path runs the same detector set for one ticker when outside protected windows; inside a protected window it defers to this sweep. The sweep is idempotent, reuses existing per-purpose budgets, degrades per item on transient transport failures (defer + tally + retry next run), and keeps hard stops loud. Slot is clear of the 04:00 pipeline, Sat 20:00 model eval, Sun 09:00 brief, and Sun 10:30 grading. |

| myclaw `weekly_review` (cross-repo: `scratch\myclaw\cron`) | Sun 14:00 | scorer (Haiku) + domain reviews (Sonnet) | slot chosen clear of Sun ~10:30 eval rungs and the morning fleet |
| myclaw `monthly_curate` (cross-repo: `scratch\myclaw\cron`) | monthly, 1st Sat 14:00 | curate (Sonnet) + threads/fitter (Fable, Sonnet fallback) | per-item degrade in-prompt; deterministic git commit outside the LLM |
Keep this table current — it is the collision surface an orchestrator checks before
launching a wave or adding a cron.

## Incident note — the standup judge's "9 of 12 suppressed" (2026-07-11 audit, PR2)

The `eval_judge` calls behind the standup's suppressed briefs were NOT genuine
low-quality verdicts — every one of them was exactly this directive's rule 3
failure signature: `CalledProcessError` exit 1 from `claude.CMD`, immediately
followed by the `LLM_FALLBACK_DISABLED=1` `RuntimeError` (see `llm_calls`,
`purpose='eval_judge'`, 2026-07-03 onward). `standup.gate.judge_item` correctly
classifies this as non-hard-stop and returns a `score=0.0` sentinel — but until
PR2, that sentinel was recorded under `STATUS_SUPPRESSED_EVAL`, which is in the
dedup set, so each transient failure locked its trip out of retry for a full
`dedup_days` (7-day) window. `standup.ledger.STATUS_JUDGE_FAILED` (excluded from
dedup) now fixes the mis-recording.

**Follow-up — root cause closed (2026-07-13, amended same day).** The dominant
cause was NOT quota: a **User-scope `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`**
(left behind by the interactive Headroom-proxy setup, ~2026-06-28) rerouted every
scheduled `claude -p` subprocess to a proxy that only runs during interactive
sessions. That is why EVERY purpose failed identically around the clock
(~237 errs/day, 07-05→07-13, successes clustering 21:00-24:00 PT when a session
had the proxy up), and why each call hung ~180-200s before exit 1 (connection
retries against a dead port — a quota refusal fails fast). Fixed 2026-07-13:
the User-scope env var removed (`ch`/`claude-headroom.cmd` sets it process-scoped
itself, so interactive Headroom is unaffected), and `src/llm/cli.py` now strips
an inherited `ANTHROPIC_BASE_URL` from the subprocess env entirely — rerouting
this app's transport requires the explicit `ES_CLAUDE_BASE_URL` (see
`_subprocess_env`). NOTE the process-scoped recurrence risk this guards against:
a dashboard/poller restarted from inside a `ch` session inherits the proxy URL
and breaks identically the moment the proxy dies.

Two genuine (secondary) scheduling problems were found and fixed in the same
investigation — real rule-1 pressure on Sundays, but not the outage's cause:

1. **Registry/reality drift.** This table documented the weekly eval rung at "Sun
   ~10:30", but the live Windows Task Scheduler `grade_calibration` entry was
   actually registered at Sun 03:30 — squarely inside the 03:00-05:00 protected
   window, and on Sundays landing on top of `run_morning_pipeline`'s own 04:00 run.
   Re-registered to 10:30 on 2026-07-13 (`Set-ScheduledTask`), matching the
   documented intent.
2. **Undocumented LLM leg.** `run_morning_pipeline`'s stage 1b (`run_standup.py`,
   added after this table was last updated) fires its own `ask_answer` +
   `eval_judge` calls daily, ~20-50 min into the 04:00 run — inside the same
   protected window but never listed in this registry. Now added to the
   `run_morning_pipeline` row above.

Also fixed: `src/llm/cli.py` and `src/llm/fallback.py` were logging only
`CalledProcessError`'s generic `str()` ("returned non-zero exit status 1"),
discarding the subprocess's captured `stderr`/`stdout` — every historical
`llm_calls.error` row was indistinguishable regardless of cause. Both now append
a stderr/stdout tail, so a future quota incident (or any other CLI failure)
self-diagnoses from the ledger without a live investigation.

## Addendum — July-2026 quota postmortem (traffic audit 2026-07-25, owner-directed)

Ledger-measured (successful prod calls only; measurement scopes excluded):

* **App demand did NOT grow.** July successful calls 2,917 / $644 vs June 3,372
  / $602. The apparent 2.3x call-count jump was retry churn against the dead
  window (e.g. `saydo_commitment_extract` 10.4 calls per unique prompt ≈ one
  re-attempt per failing day) — eliminated going forward by the #1008 breaker.
* **The app-side burn spike was one purpose-era**: `news_structuring` on Opus
  with ~206K cache-read tokens/call — $366 of July's $644 (57%), 197M of the
  222M tokens in the 04:00 PT pipeline hour. **Fixed 2026-07-19 by #943**
  (Sonnet pin + incremental persist): ~120 calls/day → ~11/day, ~$45/day →
  ~$2.8/day.
* **Exhaustion timing matched app burn + interactive load**: both dead bands
  (Jul 5-13, Jul 20-23) began immediately after multi-day 25-50M-token app
  days combined with the month's exceptional interactive/agent-wave activity
  (the #941-#1018 build program). Interactive usage is invisible to the ledger
  but shares the same subscription window.
* **No runaway consumers found.** `saydo_commitment_extract` (new Jul 2),
  `recent_developments`, `earnings_themes_split` are all 1-call-per-item,
  change-gated; their spikes are legitimate backlog flushes on the first
  healthy day after a dead band.
* **One residual risk — the recovery flash-flood**: the first healthy day
  flushes the whole deferred backlog at once (Jul 14: 462 calls / 43M tokens),
  which can re-stress a freshly reset window. Open suggestion: cap deferred-
  backlog processing per run so a flush spreads over 2-3 runs.
* Schedule placement audited against the live Task Scheduler registry: LLM burn
  concentrates 04:00-07:00 PT (asleep hours, window recovers before interactive
  time); weekend rungs segmented. One optional decongestion (needs elevation —
  non-admin cannot modify the registered task): move `refresh_business_factors`
  Sun 11:30 → Sun 19:30 to thin the 4-job Sunday-morning window
  (`schtasks /change /tn "\earnings-summary\refresh_business_factors" /st 19:30`
  from an elevated shell, then mirror `cron/refresh_business_factors.task.xml`).

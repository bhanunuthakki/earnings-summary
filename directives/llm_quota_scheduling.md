# LLM quota & scheduling discipline (agent bursts × cron fleet)

**Why this exists.** The app's in-app LLM transport (`src/llm/cli.py` → subscription
`claude` CLI) shares **one Pro/Max session quota** with every interactive Claude Code
session on this machine. On 2026-07-02 and 2026-07-03 two consecutive all-night
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
| `run_morning_pipeline` | daily 04:00 | stage 0 news → `material_news_classification`; stage 0b `decision_conditions_extract` (+ qualitative twin) | the canonical protected window; post-#814 degrades per-item |
| `refresh_scenario_priors` | monthly, 1st 03:00 | `scenario_prior` (Sonnet) | `--only-changed` + `inputs_sha256` → usually zero calls |
| weekly eval rungs (`run_grade_calibration` / model-eval) | Sun ~10:30 | `eval_judge`, rubric audits, sweep candidates | daytime slot — deliberately outside burst hours |
| `ledger_synthesis` | daily (morning block) | `theme_synth` | cost-capped; degrades to "synthesis not available" |
| capture poller (service, event-driven) | continuous | `capture_intent` (Haiku per musing), `artifact_brief` (Sonnet) | no fixed window; starved calls surface as missed classifications — budgets seeded warn-mode (0138/0139) |
| `coach_pings` | daily 07:15 | **none** (zero-LLM governor) | listed to show it's quota-safe |
| `run_red_team` | monthly, first Saturday 10:00 | `red_team_attack` (per held name, rotating lens), `red_team_cross_book` (factor-block / style-drift / human-capital, 3 calls) | daytime weekend slot, clear of the fleet's protected windows; ~15 calls/run; per-item degrade (directives/monthly_red_team.md Phase 2); Windows Task Scheduler has no native Nth-weekday trigger, so the task fires every Saturday and the script itself no-ops unless it's the month's first (`execution/run_red_team.py::is_first_saturday`) |

Keep this table current — it is the collision surface an orchestrator checks before
launching a wave or adding a cron.

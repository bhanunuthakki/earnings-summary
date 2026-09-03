# LLM quota and scheduling registry

This runbook is the single current repository authority for collision avoidance among scheduled application LLM work and interactive agent bursts. Global delegation and quota principles come from `agent-operations.SCHEDULING.md`; `src/llm/cli.py` owns transport behavior. Historical incidents live in `history/llm_quota_incidents_2026_07.md` and are evidence, not current procedure.

## Scheduling contract

- Timezone is `America/Los_Angeles`.
- Protect 03:00–05:00 daily. A material interactive or multi-agent burst must finish before that window; space planned bursts at least 6–7 hours apart.
- Register every new scheduled LLM leg in the table below in the same change. Verify the live scheduler against the checked-in task definition before relying on its time.
- Scheduled work degrades per item on transient membership-transport failure: persist an explicit deferred tally and retry next run. Setup failures and configured hard budget stops remain loud and nonzero.
- Resume a quota-interrupted agent when its context remains valid; do not repeat completed work.

## Current window registry

| Job | Schedule | LLM work / constraint |
|---|---|---|
| `run_morning_pipeline` | daily 04:00 | news classification, decision-condition extraction, standup composition/judging, eligible pre-earnings briefs, and portfolio-only post-earnings readouts; canonical protected window |
| `refresh_scenario_priors` | monthly, 1st 03:00 | changed-input scenario-prior refresh |
| `weekly_p2_lens_refresh` | Friday 22:00 | P2 reread and thesis-drift lenses; dispatch ends by 01:35 |
| `tenet_accountability` | Saturday 09:00 | changed-tenet accountability calls |
| `run_red_team` | first Saturday 10:00 | held-name and cross-book attacks |
| `thesis_collision` | Saturday 11:00 | whole-book clustering, cached on thesis-set hash |
| `disclosure_change_sweep` | Saturday 14:00 | changed-accession detector and materiality gate |
| `run_weekly_model_eval` | Saturday 20:00 | candidate sweep, judging, prompt A/B, and behavior distillation |
| `run_weekly_packet` | Sunday 08:00 | per-item packet predrafts |
| `compose_senior_partner_brief` | Sunday 09:00 | one governed weekly synthesis |
| `run_grade_calibration` | Sunday 10:30 | grading calibration and rubric audits |
| `refresh_business_factors` | Sunday 11:30 | changed-input business-factor taxonomy |
| `ledger_synthesis` | daily morning block | cost-capped theme synthesis |
| `capture poller` | continuous/event-driven | bounded capture, triage, reply-intent, and research-routing calls |
| `fetch_news` quality scoring | with news fetch | bounded batches riding the morning-pipeline window when scheduled |
| `session_distill` | manual only | one session-distill purpose; postmortem backfill is a separate explicit action |
| `coach_pings` | daily 07:15 | no LLM work |
| `run_decision_nudge` | daily 17:00 | no LLM work |
| myclaw `weekly_review` | Sunday 14:00 | cross-repository scorer and domain reviews |
| myclaw `monthly_curate` | first Saturday 14:00 | cross-repository curation and fitting |

## Registration evidence

For a new or moved job, record the checked-in task definition, verified live schedule, purposes invoked, expected maximum duration, budget behavior, transient-degradation behavior, and adjacent windows. If any are unknown, do not claim the slot is collision-free.

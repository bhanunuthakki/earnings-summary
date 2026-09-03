# July 2026 LLM quota and scheduling incidents

Historical evidence only. The current registry and procedure live in `../llm_quota_scheduling.md`.

- Consecutive all-night agent waves exposed the need to separate material bursts by 6–7 hours and protect the 03:00–05:00 morning-pipeline window.
- A July 11 investigation showed that apparent quota failures were primarily caused by an inherited `ANTHROPIC_BASE_URL` pointing scheduled subprocesses at an unavailable interactive proxy. The transport now requires an explicit application-specific override and records useful sanitized failure tails.
- Live Scheduler state had drifted: grading ran Sunday 03:30 although documentation expected 10:30. The task and checked-in definition were reconciled, motivating the live-versus-declared verification rule.
- A previously undocumented standup LLM leg collided with the protected window, motivating the complete registry requirement.
- July traffic analysis found retry churn and one expensive news-structuring purpose rather than a general demand increase. The purpose was repinned and made incremental. Deferred-backlog recovery remains bounded by each job's implementation and budget.

Do not recover current schedules or model pins from this history; inspect the registry, scheduler definitions, routing configuration, and current evaluation receipts.

# Allocation skill delivery — September 8, 2026

## Delivered scope

The [next-dollar-allocation skill](../../src/advisor/skills/next-dollar-allocation/SKILL.md)
packages the existing analyst sequence: current holdings, goals, account taxes,
permitted funding actions, deterministic arithmetic and one preferred plan with
explicit evidence limits. A new optimizer is not a prerequisite; app routing and
frontend integration remain later work.

The public source includes the skill, invocation metadata, an interface reference,
[method review](allocation_method_review_2026-09-08.md),
[future design reference](../architecture/capital_allocation_workflow.md), and the
[canonical next-dollar reference](../../directives/next_dollar_model.md).
Private portfolio details, recommendations and execution follow-up remain in the
existing local session record and the authorized Linear handoff. They are deliberately
excluded from the published tree and its new commit history.

## Invocation

Ask “Run my next-dollar allocation,” or invoke `$next-dollar-allocation` from the
agent with the skill installed. The local installation links the standard Codex
skills directory to the repository-owned skill above. It carries forward current
saved goals and permissions; an override such as “cash only this time” narrows that
run without rewriting durable preferences. No new app `/next-dollar` command or
automatic in-app routing is claimed.

## Remaining work

- BHA-149 owns the classification and full-book coverage defects. Missing coverage
  limits complete quantitative claims without preventing supported scoped advice.
- BHA-150 owns later app/frontend integration. An optimizer remains optional future
  work if demonstrated shortcomings justify it.
- BHA-151 retains the separate evaluation and full-brief follow-up; its operational
  status and private job evidence belong in Linear.

No portfolio write, trade or monitoring schedule is part of skill delivery.
An execution report must be reconciled against current authoritative holdings before
recording a target as achieved. The owner controls session archival.

## Validation boundary

Skill metadata, installation resolution, local document links and referenced code
interfaces were checked. Packaging does not establish that a fresh production
allocation was run or that a complete quantitative optimizer was validated. The PR
and Linear delivery receipt record release checks and merge status.

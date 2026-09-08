# Existing project interfaces

Resolve `repo_root` from the current earnings-summary project or the real skill
path (the skill is under `src/advisor/skills/next-dollar-allocation`). Read
`AGENTS.md` and `directives/agent_host_operations.md` there. Production identity and
DB paths come from approved runtime configuration, never a remembered hostname or
checkout-default `data/portfolio.db`. Respect another task's runtime write window.

## On the canonical host or an approved explicit snapshot

Use the configured project Python and existing modules. Pass the explicit database
path where supported, and resolve the configured path before an interface that uses
the environment. Do not invoke checkout-default legacy entrypoints unchanged.

| Need | Existing owner |
| --- | --- |
| Full current positions, per-account values and tax treatment | `integrations.portfolio_tracker_client.fetch_live_portfolio`; prefer the configured typed v1 transport and its coverage envelope |
| Stable security/account identity and cash flags | `integrations.portfolio_tracker_v1.TrackerV1Client` positions, securities and accounts reads |
| Reconciled cash/investment projection | `integrations.portfolio_allocation.fetch_portfolio_allocation`; preserve unresolved constituents and reasons |
| Current affirmed owner context | `owner_profile.store.list_facts(conn, status="affirmed")` with a read-only explicit DB connection |
| Recorded sizing and positioning | `user_state.sizing.latest_intent`, `positioning.store.latest_intent`, `positioning.target.resolve_target_context` |
| Position facts and deterministic tax pre-analysis | `advisor.position_review.build_pre_analysis(repo_root, ticker, db_path=explicit_path)`; alternatively `execution/review_position.py TICKER --json --db <resolved-path>` with the approved tracker origin |
| Lot-based tax calculation | `advisor.position_tax.build_position_tax_view`; use existing validated reconstruction/history and configured tax profile |
| Risk and candidate comparisons | `allocation.book_risk`, `allocation.what_if`, materialized candidate-fit and ETF workups |
| Company-reported facts | Local micro-thesis, canonical provenance-aware financial readers and transcripts; use the project's source policy |

## Remote read-only dashboard access

Resolve the exact approved private dashboard origin from current host authority.
Use these existing GET routes when direct canonical-host interfaces are unavailable:

- `/api/work-os/portfolio`: NAV, research holdings and aggregate classification.
  It is **not** a complete per-account portfolio export.
- `/advisor/sizing-intents/<TICKER>`: recorded target, narrative, checkpoint and
  verification state. An unverified checkpoint is not proof the target was attained.
- `/api/peek/review/<TICKER>`: deterministic position/tax pre-analysis. Ticker is a
  path segment, not a `?ticker=` query. This may be slow; bound reads and failures.
- `/api/peek/etf_workup?ticker=<TICKER>`: existing ETF evidence.
- `/api/allocation/recommendation` (GET): prior incremental-dollar artifact. Treat
  it as a dated narrow result, not a full rebalance execution.

If full account holdings cannot be reached through these projections, use a
provenance-bearing export through the live host owner. Do not infer omitted small
holdings, expose the tracker listener, or claim all positions were evaluated.

## Important call traps

- App chat `/review` can start a full LLM verdict job. For evidence-only work use
  the pre-analysis function or GET peek, not that chat command or `--verdict`.
- `/actions/refresh`, advisor memo generation and recommendation POSTs are not
  required to run this analyst skill. They can launch LLM/provider/persistence work.
- `advisor.context.build_advisor_context` and some legacy CLIs still infer a
  checkout-local DB. Prefer the individual explicit-path readers above.
- `review_position.py` requires an explicit `--db` argument: its argparse default
  is checkout-local and does not defer to the environment. Pass the configured
  tracker origin through `--api-url` when needed; do not infer a loopback service.
- The existing next-dollar frontier models external cash, has limited ETF/sale
  support, and mixes ranking scales in its diversifier picker. Do not use it as a
  complete optimized trade list or trust its existing-cash/partial-funding math.
- An ETF review's missing corporate thesis/DCF is not a sell signal. Likewise, a
  partial cash classifier does not make known money markets unknowable assets.

For methodology questions, read `docs/research/allocation_method_review_2026-09-08.md`.
For owner decisions, use current private owner context or the authorized Linear handoff.
`docs/research/allocation_session_handoff_2026-09-08.md` records public delivery status only.
The larger optimizer/frontend proposal is in `docs/architecture/capital_allocation_workflow.md`;
its future enhancements are not prerequisites for using this skill.

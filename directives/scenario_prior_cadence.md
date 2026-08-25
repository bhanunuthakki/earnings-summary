# Directive: scenario-prior refresh cadence (no re-call per quarter)

**Status: SHIPPED 2026-07-02.** Feature 2's PR D/#777 + PR E/#782 built the per-name
DCF scenario prior (`dcf.scenario_prior`, `execution/set_scenario_priors.py`) but never
wired a re-run cadence — the producer was a one-shot manual invocation. This directive
records the cadence design and the hydration it unblocked.

## Problem

`dcf.scenario_prior` is a governed but RISKY-adjacent LLM call (it moves allocation via
`dcf.scenario_reward`). The owner's stance, from the module docstring: priors are stable
quarter-to-quarter, grounded in the thesis/bear-case anchors — re-calling the LLM on an
unchanged thesis just burns spend for the same answer. Every other cache-worthy LLM seam
in this codebase (`dcf.compute.peer_selection`) already solves this with an
`inputs_sha256` cache-invalidation pattern: the digest is the Content Identity of
the canonical inputs, and a hit skips the call.
`set_scenario_priors.py` had no such gate — a naive cron re-run would call the LLM for
all ~42 DCF-universe names every tick, forever, even for names whose thesis hasn't
moved in months.

## Design (mirrors `peer_selection`)

- `dcf.scenario_prior.anchor_block_for_ticker(ticker, repo_root)` — the same
  thesis/bear/priors anchor composition `set_scenario_prior_for_ticker` already builds,
  split out so it can be hashed WITHOUT spending a call.
- `dcf.scenario_prior.anchor_inputs_sha256(anchor_block)` — `sha256` of that text.
- `set_scenario_priors.write_scenario_prior_to_json` now stamps `inputs_sha256` onto
  every written `scenario_prior` block (alongside `bull_weight`/`base_weight`/
  `bear_weight`/`rationale`/`set_by`/`as_of`).
- `execution/set_scenario_priors.py --only-changed`: for each name, hash the CURRENT
  anchor text and compare to the block's stored `inputs_sha256`.
  - **Match** → `"skipped_unchanged"`, no LLM call.
  - **Mismatch, or no prior on file yet** → proceeds exactly as the non-cadence path
    (LLM call, write, stamp the new hash).
  - An owner-set block (`set_by == "owner"`) is untouched either way — owner wins is
  unconditional, cadence or not.

The Logical Idempotency Key is `(ticker, scenario_prior)`; the current thesis/bear
anchor and its `inputs_sha256` form the Observation Version; every governed call has
a unique Attempt Identity. The input digest is never an Attempt Identity.

## Cron wiring

New standalone monthly task (not piggybacked on the Sunday model-eval window — that
loop serves a different purpose at a different cadence and coupling them would make
either harder to reason about):

- `cron/run_refresh_scenario_priors.bat` → `python execution/set_scenario_priors.py
  --only-changed --apply --repo-root <PROJECT_ROOT>`.
- `cron/refresh_scenario_priors.task.xml` — monthly, 1st @ 03:00, `LogonType=S4U`
  (matches the current convention, #759/#790), `ExecutionTimeLimit=PT4H` (generous
  headroom for a full-universe pass on a cache-miss month).
- Registration (`schtasks /create /f`) from a live Windows session is **pending** —
  the `.task.xml`/`.bat` pair exists in the repo but has not yet been registered with
  Task Scheduler as of this directive.

## Hydration (one-time, unblocked by this + the mirror re-audit)

`execution/set_scenario_priors.py --all-named --apply` (no `--only-changed` — first
run, nothing on file to compare against) then `execution/refresh_dcf.py --all-named`
populate the ~42-49 DCF-universe names' `data/dcf_assumptions/<T>.json` +
`dcf_runs.assumption_snapshot_json` with real `scenario_prior`/`priced_in` blocks for
the first time. See the PR description / session record for cost and per-ticker
verification detail.

## Related

- `directives/peer_selection_llm.md` — the pattern this mirrors.
- `directives/model_eval_loop.md` — the OTHER weekly Sunday window; deliberately not
  reused here (different purpose, different cadence).

# Cheapest-at-parity routing

**Class:** canonical. This file owns economic eligibility and ordering for LLM
runtime candidates. `llm_evals.md` owns quality evidence, `model_eval_loop.md` owns
qualification and promotion, and `llm_calls.md` owns production dispatch and fallback.

## Policy

For each retained purpose, use the lowest total-cost complete runtime configuration that
meets the purpose CapabilityProfile, clears deterministic and semantic quality gates,
stays within reliability/latency bounds, and preserves the required degradation path.
Cheapest never means cheapest model name, token price, or provider in isolation.

## Economic authority

- `src/llm/model_ladder.py` owns the executable candidate order, cost coordinates,
  provider-family mapping, and ladder fingerprint.
- `llm_calls` owns observed charged cost, token use, latency, failure, and fallback
  attribution when the transport exposes them.
- The dated shared model-frontier reference may nominate candidates and refresh external
  list-price facts; it cannot qualify or promote them.
- `src/llm/model_eval.py` combines economic eligibility with task evidence.
- `execution/apply_model_switches.py` is the only automatic production switch writer.

Do not copy model IDs, provider incidents, price tables, or workstation paths into this
directive. A changed registry value creates a new executable fingerprint and requires
fresh applicability/evaluation evidence where it can alter ordering.

## Total-cost comparison

Compare complete candidates on the cost the product would actually bear at the purpose's
representative workload:

- input, output, cached-input, long-context, tool/grounding, and retry charges;
- expected fallback duplication and failure/replay rate;
- p50 and tail latency against the product bound;
- for locally served or open-weight candidates: hardware acquisition/amortization,
  electricity, memory/storage, serving/observability labor, availability, and throughput;
- subscription or quota opportunity cost when a shared pool can starve protected work;
- migration and operational burden when material to the decision.

Use charged ledger data when available. Otherwise use a dated, attributable estimate and
label its uncertainty. Unknown cost is unknown, never zero. Avoid false precision: a
small modeled saving does not justify a switch whose evidence, reliability, or maintenance
cost is uncertain.

## Capability-first eligibility

Before ranking a candidate as cheaper, require:

1. the complete runtime configuration is registered;
2. its typed model CapabilityProfile satisfies context, vision, and structured-output
   requirements, while separately registered transport/runtime evidence satisfies
   tool/grounding, privacy, and deployment requirements;
3. representative eval coverage exists at the product's current stage;
4. deterministic schema/business gates pass;
5. semantic quality and failure evidence clear the retained promotion thresholds; and
6. the fallback/degradation path remains attributable and no less safe.

A benchmark score, provider reputation, model-family label, or lower list price satisfies
none of these gates by itself. Provider/model diversity is optional unless a registered
risk, evaluation design, or owner request makes it load-bearing.

## Decision and routing boundary

The economic policy nominates only candidates that are cheaper than the incumbent under
the registered workload and complete-cost method. `model_eval_loop.md` then decides
`SWITCH_DOWN`, `KEEP_INCUMBENT`, or `HOLD`. Qualifying retained evidence may cause
`execution/apply_model_switches.py` to write an active model-pin override; production
resolution remains in `src/llm/resolver.py`.

Operational fallback is not a cheaper-model promotion. Each attempted leg remains
separately ledgered, and fallback cost/reliability feeds the next comparison. A provider
runbook may explain setup mechanics but cannot change this policy or declare a candidate
qualified.

## Stage and refresh cadence

- Exploration: prefer the smallest reversible candidate set; rough cost estimates may
  guide experiments but never production promotion.
- Recurring personal use: use representative observed workload, bounded evals, and
  measured failure/latency/cost before a switch.
- External/commercial use: ratify cost and quality thresholds, include operational
  burden and tail behavior, retain rollback evidence, and monitor post-switch drift.

Refresh candidate discovery when the dated frontier changes materially. Re-run economic
ordering when the ladder fingerprint, purpose workload, pricing/terms, runtime
configuration, or fallback behavior changes. Do not run a broad sweep merely because a
calendar date passed if no decision input changed.

## Identity and failure policy

- **Logical Idempotency Key:** `(purpose, incumbent configuration, candidate
  configuration, workload/economic-policy version)`.
- **Content Identity:** digests of the executable ladder, workload sample, charged ledger
  extract, and external price provenance.
- **Observation Version:** complete runtime coordinates plus price/access date, workload
  cutoff, and fallback/reliability evidence window.
- **Attempt Identity:** unique nomination/evaluation sweep and its receipts.

Missing, stale, or inconsistent cost/capability data yields HOLD or exclusion from the
ordered set. Budget/provider/parser/judge failure never makes a candidate cheaper or
qualified. Current production routing remains unchanged until the promotion owner has
complete retained evidence.

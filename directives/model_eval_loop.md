# Model evaluation and promotion

**Class:** canonical. This file owns candidate qualification, promotion, regression
reversion, and the evidence state connecting evaluation to production routing.
`llm_evals.md` owns task-quality evidence, `cheapest_model_routing.md` owns economic
ordering, and `llm_calls.md` owns live dispatch.

## Outcome

A purpose changes runtime configuration only after a cheaper complete candidate clears
the same task contract as the incumbent. Outage, missing coverage, judge failure, or
ambiguous evidence yields HOLD; it never qualifies a candidate or silently preserves a
stale pass.

## Executable authority

- `src/llm/model_eval.py`: candidate runs, deterministic checks, judge aggregation,
  and `SWITCH_DOWN` / `KEEP_INCUMBENT` / `HOLD` recommendations.
- `src/llm/backend_judge.py`: brand-blind pairwise judgments.
- `execution/run_model_eval_sweep.py`: scheduled/interactive evaluation sweep.
- `execution/apply_model_switches.py`: the only automatic promotion/demotion writer.
- `model_eval_verdicts`: append-only evidence and recommendation history.
- `model_pin_overrides`: active production routing override and retained history.
- `src/llm/resolver.py`: consumer of the active override.

Provider and model IDs, candidate family, and cost ranks come from executable registries.
They are inputs to evaluation, never evidence that a candidate is capable.

## Candidate contract

A candidate is the complete runtime configuration actually evaluated: model artifact or
API ID, provider/serving adapter, quantization when applicable, structured-output mode,
context settings, hardware/runtime for open-weight models, retry policy, and timeout.
Changing any load-bearing element creates a new candidate Observation Version.

Before spend, the sweep must prove:

1. the purpose and incumbent have representative cases and a task-specific rubric;
2. the candidate satisfies the purpose CapabilityProfile;
3. the candidate is economically eligible under `cheapest_model_routing.md`;
4. prompts, source inputs, and deterministic grading contracts are versioned; and
5. judge configuration is registered and independent of contestant labels.

## Evaluation sequence

1. Freeze the case set, prompt version, incumbent/candidate configurations, rubric,
   deterministic gates, and cost registry fingerprint.
2. Run both contestants on identical inputs. Parse/schema/business-invariant failures are
   candidate errors, not low-quality prose to be averaged away.
3. Apply deterministic gates before semantic judgment.
4. Present surviving outputs brand-blind and position-balanced to registered judges.
5. Persist attempted-case counts, candidate and judge error rates, agreement, parity,
   latency, charged or estimated total runtime cost, and all evidence versions.
6. Produce exactly one recommendation: `SWITCH_DOWN`, `KEEP_INCUMBENT`, or `HOLD`.

No completed cases, excessive candidate errors, excessive judge errors, incomplete
coverage, or conflicting evidence is HOLD. Never convert infrastructure failure into a
quality result.

## Promotion and reversion

`execution/apply_model_switches.py` owns activation thresholds and consecutive-evidence
requirements. It may activate a `model_pin_overrides` row only from qualifying retained
`SWITCH_DOWN` verdicts that share compatible evidence versions. It may deactivate the
override after the configured retained `KEEP_INCUMBENT` regression evidence. HOLD never
switches or reverts production.

The writer is single-owner and auditable. A prose edit, provider runbook, eval runner, or
judge cannot directly change production routing. Manual activation/deactivation remains
an explicit owner action and preserves history.

## Stage calibration

- Exploration: a small smoke comparison may nominate a candidate but cannot promote it.
- Recurring personal use: representative cases, deterministic failures, retained
  pairwise evidence, cost/latency data, and tested degradation are required.
- External/commercial use: ratified thresholds, calibrated judges, release monitoring,
  rollback evidence, and statistically appropriate coverage are required.

Increase sample and judge breadth because observed error, decision consequence, or
uncertainty warrants it—not because a provider is new.

## Identity and failure policy

- **Logical Idempotency Key:** `(purpose, incumbent configuration, candidate
  configuration, evidence-set version, evaluation-policy version)`.
- **Content Identity:** digests of case corpus, prompts, contestant outputs, rubric,
  executable ladder, and judge configuration.
- **Observation Version:** complete contestant/runtime coordinates plus all Content
  Identities and the evidence cutoff.
- **Attempt Identity:** evaluation `run_id` and call receipts for one sweep. Retries
  receive new Attempt Identities.

Budget exhaustion, transport/setup error, parser/schema failure, missing judge evidence,
or unverifiable cost is retained and surfaced. The result is HOLD unless deterministic
evidence already proves the candidate fails, in which case it may be KEEP_INCUMBENT;
neither path manufactures parity.

## Verification

For a change to this loop, run the focused model-eval, backend-judge,
apply-model-switches, resolver, and ledger tests. A live sweep is additional evidence,
not a substitute for deterministic proof; if it is unavailable, report that explicitly.

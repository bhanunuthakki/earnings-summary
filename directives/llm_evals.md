# LLM evaluation contract

**Class:** canonical. This file owns representative quality and failure evidence
for an application LLM purpose. `llm_calls.md` owns execution and telemetry;
`model_eval_loop.md` owns cheaper-model promotion; `cheapest_model_routing.md`
owns economic policy; `meta_eval_governance.md` and `llm_evals_plan.md` are
historical build records.

## Outcome

Every retained LLM purpose has enough quality and failure evidence for its product
stage. Evaluation measures the task behavior that users rely on; it does not treat
provider reputation, a general benchmark, or a successful parse as task quality.

## Purpose contract

Before a purpose is retained, register:

- one stable purpose key and prompt version;
- typed input and output boundaries;
- representative cases, including empty, malformed, and adversarial inputs where applicable;
- deterministic schema and business-invariant checks;
- a task-specific rubric and explicit pass/HOLD rules;
- attributable fallback or degradation behavior; and
- per-call latency, cost, model, transport, failure, and prompt-version telemetry.

The purpose also declares a model `CapabilityProfile` for load-bearing context, vision,
or structured-output constraints. Tool availability, live grounding, privacy, and
deployment requirements are evaluated separately as transport/runtime constraints. A
provider/model ID may satisfy either class only through executable evidence; its family
name or reputation is never evidence.

Malformed output, missing evidence, unavailable judges, or incomplete coverage yields
`HOLD` or a labeled degraded result, never a silent pass.

## Stage calibration

| Product stage | Required evidence |
|---|---|
| Exploration or disposable prototype | Typed boundary, deterministic failures, and a small representative smoke set. Results cannot promote a production model or qualify a blocking judge. |
| Recurring personal use | Representative corpus, task rubric, cost/latency/failure evidence, and tested degradation on the decisions the owner relies on. |
| External or commercial use | Ratified thresholds, calibrated graders where semantic judgment is required, release gates, monitoring, and retained versioned evidence. |

Increase case count and judge breadth because observed error, consequence, or decision
complexity warrants it—not merely because another model or provider is available.

## Model neutrality

Evaluate candidates brand-blind where practical. A candidate is the complete
model/runtime/provider configuration actually used, including open-weight hardware,
quantization, serving stack, tail latency, and operating cost. Provider or model-family
diversity is optional unless a registered risk, evaluation design, or owner request
requires it.

## Evidence owners

- `src/evals/coverage.py`: purpose-to-evaluation coverage.
- `evals/`: versioned cases, goldens, and rubrics.
- `execution/run_llm_evals.py`: application quality runs.
- `src/llm/backend_judge.py`: brand-blind pairwise judgment.
- `src/llm/model_eval.py`: candidate evaluation mechanics.
- `llm_calls`: attributable production and evaluation telemetry.

Prompt changes follow the versioned regression workflow in `llm_calls.md`. Model
promotion follows `model_eval_loop.md`; judges never switch production routing directly.

## Identity and evidence failure

- **Logical Idempotency Key:** `(purpose, eval-suite version, candidate configuration)`.
- **Content Identity:** digests of cases, rubrics, prompts, outputs, and judge config.
- **Observation Version:** complete runtime coordinates, prompt/source versions, and
  evidence cutoff.
- **Attempt Identity:** unique eval run and call receipts; retrying changes it.

Missing cases, unavailable graders, parse/schema failure, incomplete runtime coordinates,
or unattributable telemetry yields HOLD. No evidence component may silently reuse an
Attempt Identity as a content or logical key.

# LLM call boundary

**Class:** canonical. This file owns live call entry, model/backend resolution,
transport fallback, structured-output behavior, budgets, and call attribution. It
does not qualify candidate quality or promote cheaper models; `llm_evals.md`,
`model_eval_loop.md`, and `cheapest_model_routing.md` own those decisions.

## Outcome

Every application LLM call crosses one governed facade, declares a purpose, resolves
against a capability profile, produces attributable telemetry, and fails or degrades
in a way the caller can distinguish from a valid empty result.

## Executable authority

- `src/llm_client.py`: public facade.
- `src/llm/cli.py`: call implementation, purpose registry, transport dispatch,
  budgets, retries, capture, and ledger integration.
- `src/llm/resolver.py`: single model/backend resolution and capability validation.
- `src/llm/model_ladder.py`: executable provider-family and cost registry.
- `src/llm/structured.py`: schema-oriented response parsing and bounded repair.
- `src/llm/prompt_registry.py`, `src/llm/prompt_versions.py`, and
  `src/llm/prompt_ab.py`: prompt attribution, versioning, and governed overrides.

Provider names, model IDs, capability entries, and prices are executable or dated
registry facts. This directive never qualifies a model by reputation or duplicates
those registries.

The typed model `CapabilityProfile` covers only intrinsic context length, vision, and
structured-output support. Tool availability, live grounding, privacy, and deployment
constraints are separate transport and evaluation gates; they must not be inferred from
the model profile.

## Call contract

Use `llm_client.call_llm` or the corresponding governed web/structured facade. New
calls must:

1. pass a stable `purpose` registered in the executable purpose registry;
2. define typed inputs and outputs, including valid empty and failure states;
3. provide a `CapabilityProfile` when context, vision, or structured-output support is
   load-bearing;
4. keep prompt text attributable to a prompt version and Content Identity;
5. pass ticker/scope metadata when applicable; and
6. handle typed budget, setup, transport, and parse failures without manufacturing a
   successful empty value.

Direct provider clients are allowed only inside registered adapters under `src/llm/`.
Call sites do not add ad-hoc provider retries, model IDs, fallback chains, or open-ended
response parsing.

## Resolution and fallback

`src/llm/resolver.py` resolves in this order:

1. explicit model argument;
2. active database model-pin override for the purpose;
3. the purpose entry in `LLM_MODELS`; then
4. the executable default.

An explicit backend wins; otherwise the registered model family selects the provider.
For normal purpose-resolved Claude-family pins, the production default is `codex`:
the Codex membership transport runs first and an operational failure falls back to the
Claude subscription transport. `LLM_PRIMARY_SUBSCRIPTION_BACKEND=claude` is the
documented rollback switch.

Registered explicit provider-family model IDs route to that provider. A model-routed
provider adapter may use the operational fallback implemented in `src/llm/cli.py`,
with both legs attributed separately. A forced backend must fail rather than silently
switch; any explicit emergency escape hatch is an operator action, not normal routing.
Setup, authorization, budget, capability, and schema failures never become a successful
fallback result.

Every resolved model and actual fallback model must have an entry in the executable
capability registry. Unknown capability metadata fails closed even when the caller has
no additional profile requirements.

`call_llm_with_web` uses the same purpose resolution and Codex-first subscription order.
Its live-grounding requirement is enforced by code; a response that lacks required
source evidence is a failure, not a grounded answer. With the default
`require_grounding=True`, exhausted web transports raise and never return plain uncited
output. Only an explicit `require_grounding=False` call may use the attributable legacy
plain-output degradation.

## Structured output

JSON-expecting call sites use `llm.structured.call_llm_structured` with an explicit
object/array shape and required keys. One bounded repair may explain the parse failure.
The structured facade always adds `requires_structured_output=True` to the caller's
profile and preserves that effective profile across repair and escalation attempts.
Final failure raises `StructuredParseError`; it never returns `{}`, `[]`, or `None`
unless that value is a schema-valid product result.

## Identity and telemetry

- **Logical Idempotency Key:** product purpose plus the durable business effect the
  caller intends; it is owned by the calling directive.
- **Content Identity:** prompt-body/input/output digests captured by the prompt and
  call ledgers.
- **Observation Version:** prompt version, source-data versions, resolved runtime
  configuration, and knowledge time used for the call.
- **Attempt Identity:** `run_id` or the unique call receipt for one execution. It
  changes on retry and is never the Logical Idempotency Key.

Every attempted leg records purpose, resolved model and backend, prompt version/digest,
latency, cost or billing class when observable, outcome/failure class, fallback
attribution, and Attempt Identity. Missing ledger or budget infrastructure fails closed
where the call would otherwise authorize spend or a durable decision.

## Change workflow

- New purpose or material prompt change: register the purpose, bump its prompt version,
  add representative eval coverage, and run the purpose-specific regression command in
  `execution/run_llm_evals.py`.
- Transport/resolver change: test family resolution, forced-backend failure,
  operational fallback attribution, budgets, and structured failure behavior.
- Model switch: do not edit prose here to qualify it. Follow `model_eval_loop.md`; the
  executable override is the production change.
- Prompt experiment: use the executable prompt experiment/override mechanism. Retain
  brand-blind evidence and reconcile a proven override back into versioned source.

CI never substitutes a live model call for deterministic tests. Unavailable live eval
evidence is reported as unavailable or HOLD, not inferred from a green unit suite.

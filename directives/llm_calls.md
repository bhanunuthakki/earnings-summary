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

## Financial narrative grounding

This contract applies to all application LLM financial narratives: free-form Ask
and Copilot answers, report-comment answers, Ledger responses, coaching, company
and portfolio reports, briefs, evaluations, assessments, earnings summaries,
cached synthesis, advisor memos and scheduled output. It also applies when an
agent assembles a standardized report without calling the application's LLM.
No service, model, transport, cache or template may lower this boundary silently.

### Before generation

Resolve the issuer, requested fiscal periods, knowledge cutoff, user scope and
required claim classes. Read the existing verified corpus through the shared
provenance-aware resolver. Row presence, provider retrieval, confidence and a
source link do not establish admission. Preserve metric definition/revision,
source observation and document/locator, fiscal start/end and period type,
currency, unit/scale, scope, accounting basis, precision, and actual versus
guidance/consensus/assumption status in the model context. Derived values need
their operands, compatible definitions and reproducible calculation.

If the required corpus is insufficient, flag the missing evidence early. Before
new collection or a response with lower coverage, obtain the owner's agreement
to the exact source classes, period window, expected effort and output limits.
An existing approval covering those facts is sufficient; do not ask repeatedly.
Use the authorized native intake routes. Do not turn a request for one year of
earnings and forward estimates into an unapproved full-history ingestion.
Unattended work defers or returns an explicit missing-evidence result when it
cannot obtain that decision; it does not invent consent.

Bounded research reduces the population to verify, not factual fidelity. A
source-checked ad hoc assessment may be useful without recurring-series admission
or complete archive coverage. Label its exact reviewed window, source basis and
gaps. It cannot claim decision-grade readiness unless the existing gates pass.
Open-ended conceptual research can proceed with visibly labeled hypotheses or
assumptions. Existing company-specific factual anchors still use verified facts;
the open-ended label never licenses an invented fact.

### Before release

Check every material factual assertion in the complete output against the exact
selected corpus, not only its cited URL or numerical tokens. Verify the metric,
period, currency/unit/scale, scope, accounting basis, comparison, qualifiers and
actual/forecast status. Replay calculations through their owning code. Use exact
source wording where a paraphrase cannot retain the qualifications safely.
Model interpretation stays visibly separate from reported facts and estimates.

The check runs before user-visible financial prose, current-cache promotion or
final artifact publication. A failed, unavailable or incomplete check leaves an
explicit draft/deferred/blocked result. Do not release a known contradiction with
only a corrective footnote. Missing rows cannot establish zero, no concentration,
no risk, no change or complete extraction.

Retain the claim/source manifest, selected corpus identity and cutoff, policy and
prompt identity, calculation evidence and verification disposition with the
output. Reuse a cache only when its evidence, scope, freshness and verification
remain valid. Historical unverified artifacts stay distinguishable; changing this
contract does not retroactively certify them.

### Implementation and evidence

`src/llm/style.py` supplies the shared factual-grounding instruction block used by
standard brief prompts and their cache identity. `src/research/method_contract.py`
loads the source-owned research method for its registered routes. The canonical
financial reader and memo verifier, and Ask's grounded/sealed retrieval and claim
checks, own their existing executable gates. Instruction delivery is not proof
that every caller uses those gates. Keep a complete call/surface inventory and
record missing adapters and bypasses explicitly until their tests pass.

Representative negative cases include a correct number with the wrong metric,
denomination, period, reporting scope or actual/forecast status; a conflicting
value; stale/restated facts; unsupported comparison; and absent evidence described
as a negative finding. Deterministic checks cover reconstructed values and
formulas. Semantic review must preserve source meaning; matching numbers or a
model's self-check alone cannot certify it. Live evaluation evidence remains
separate from mocked transport and unit-test results.

## Resolution and fallback

`src/llm/resolver.py` resolves in this order:

1. explicit model argument;
2. active database model-pin override for the purpose;
3. the purpose entry in `LLM_MODELS`; then
4. the executable default.

An explicit backend wins; otherwise the registered model family and shared fleet policy
select the provider order. Normal purpose-resolved subscription calls consume that policy
without restating a project default; an operational failure advances to the next registered
fleet adapter. The fleet's canonical primary-backend setting is the rollback switch.

Registered explicit provider-family model IDs route to that provider. A model-routed
provider adapter may use the operational fallback implemented in `src/llm/cli.py`,
with both legs attributed separately. A forced backend must fail rather than silently
switch; any explicit emergency escape hatch is an operator action, not normal routing.
Setup, authorization, budget, capability, and schema failures never become a successful
fallback result.

Every resolved model and actual fallback model must have an entry in the executable
capability registry. Unknown capability metadata fails closed even when the caller has
no additional profile requirements.

`call_llm_with_web` uses the same purpose resolution and fleet subscription order.
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

## LLM artifact lifecycle

Current artifacts and artifacts reached by a registered durable provenance, alert,
decision, prediction, calibration, call-ledger, insight-input, decision-draft, or
standup-evidence edge are retained. Only superseded artifacts with none of those durable
edges are eligible after 180 days. Malformed registered provenance fails closed.

`execution/db_gc.py` owns the executable edge inventory, cutoff constant, archive-first
mechanics, and dry-run/apply behavior. The existing weekly DB GC job is the only
retention writer; this contract does not authorize another cleanup task. Adding a
durable artifact consumer requires adding its edge before cleanup can treat the artifact
as unreferenced. Changing the 180-day default is a canonical contract change here, not a
runbook-only edit.

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

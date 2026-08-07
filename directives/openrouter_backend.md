# OpenRouter third backend (metered key, eval-gated, opt-in candidate pool)

**Purpose.** Widen the pareto optimizer's *cheap-candidate* axis. The Claude and
Gemini backends give two providers; OpenRouter is a single OpenAI-compatible
gateway to hundreds of models (DeepSeek, Qwen, Llama, Mistral, GLM, ...), many
far cheaper and increasingly at-parity on the high-volume structured-classifier
purposes. One integration reaches them all — contrast the bespoke Claude
subprocess wrapper and the Gemini SDK backend, each a full provider integration.

**Routing is model-first** (same as Gemini — see `directives/cheapest_model_routing.md`):
`call_llm` resolves a purpose's model; if the resolved id is an OpenRouter
family model (in `model_ladder.MODEL_LADDER`, or any `provider/model` slash slug),
the call dispatches to `src/llm/openrouter_backend.py` via `family_of()`.
`backend="openrouter"` forces it (the compare harness). The
`OPENROUTER_BACKEND_ALLOWED_PURPOSES` symbol exists only for symmetry with the
other backends' tests — it is dead code, ships empty, and never gates routing.

## Setup (operator)

1. Create a key at https://openrouter.ai/keys and top up credits.
2. Put it in `.env` as `OPENROUTER_API_KEY=<key>`.
3. Nothing bills it until a purpose is routed to an OpenRouter model or a call
   forces `backend="openrouter"`. Without a key, an OpenRouter-routed call fails
   fast with `LLMSetupError` (and, for a model-routed purpose, degrades to Claude
   — so a missing key can never break the pipeline).

## The model-identity guardrail (the eval-integrity requirement)

OpenRouter can serve the *same* model id from different upstream providers at
different quantizations/context limits. Left unpinned, a graded "candidate" is a
moving target: grade DeepSeek-via-A@fp16 today, production later serves
DeepSeek-via-B@fp8, and the parity verdict silently stops transferring. Every
call therefore sends a `provider` routing object (`_provider_routing()`):

| Field | Value | Why |
|---|---|---|
| `allow_fallbacks` | `false` | Never silently reroute to an unapproved provider; an outage surfaces as an honest error, not a silent identity swap. |
| `require_parameters` | `true` | Only providers that honour every request param. |
| `data_collection` | `"deny"` (default) | Keep prompts off providers that train on them. Relax with `OPENROUTER_DATA_COLLECTION=allow` for a wider/cheaper pool. |
| `quantizations` | `["fp16","bf16","fp8","unknown"]` | A precision FLOOR — excludes aggressive int4/int8 quants whose quality drifts, so the same id doesn't grade differently across calls. |
| `only` | *(empty)* | The strongest lever: a HARD upstream pin. Set `OPENROUTER_PROVIDER_ONLY=DeepInfra,Fireworks` to freeze the exact upstream for a rigorous graded eval. |

**Why this is sufficient for a transferable verdict:** eval-time and
production-time calls both go through `call_openrouter` with the *same* routing
config, so a parity verdict earned in the sweep holds in production by
construction. For a maximally rigorous grade, pin `OPENROUTER_PROVIDER_ONLY`
during the sweep and keep it pinned in production.

## Failure policy

Mirrors the Gemini backend. A model-routed OpenRouter call that fails
**operationally** (429 rate-limit, 5xx, network, malformed body, empty content,
bad-model 400) degrades to Claude so a model swap can never break the pipeline.
**Hard stops propagate** per `is_hard_stop`: `LLMBudgetExceeded` (budget gate) and
`LLMSetupError` — the latter raised by `_classify_openrouter_failure` on 401/403
(bad/missing key) and 402 (out of credits), which are deterministic and
operator-actionable. A **forced** `backend="openrouter"` call raises instead of
switching — the caller asked for that backend's answer.

## Cost + ledger

Every call writes the standard `llm_calls` row: `model` is the `provider/model`
slug (fallback_used NULL), token counts from the response `usage`, and
`cost_estimate_usd` = **OpenRouter's REAL charged cost** (`usage.cost`, returned
because the request sets `usage: {include: true}`) — more accurate than an
estimate. The `model_ladder.py` seed prices for the OpenRouter entries are
approximations used ONLY for pre-call *ranking* in the sweep; the ledger stays
accurate regardless (verify/curate ladder prices against https://openrouter.ai/models).

## Testing a candidate (the opt-in path)

OpenRouter candidates are **excluded from the automatic sweep by default** —
`cheaper_candidates(incumbent, include_openrouter=False)` — so an untested third
backend never silently floods every purpose. To grade one:

```
# Ad-hoc, through the existing brand-blind pairwise judge (Claude vs the candidate):
python execution/compare_backends.py --purpose viewspec_compile \
    --prompt-file p.txt --gemini-model deepseek/deepseek-chat   # (backend forced per-side)

# Or opt a purpose into the model-eval sweep's candidate set
# (cheaper_candidates(..., include_openrouter=True)), then grade as usual.
```

Promotion to production stays eval-gated: a purpose reaches OpenRouter only after
the judges grade its output at parity, at which point you pin the OpenRouter id in
`LLM_MODELS` / `model_pin_overrides` (model-first routing does the rest).

## Data governance

Prompts flow through OpenRouter's infrastructure and the chosen upstream. Owner
stance (2026-07-02): fine for this platform's research prompts, provided **no SSN
/ bank-account / PII-type data** is ever in a prompt — none is today. The
`data_collection: "deny"` default additionally keeps prompts off providers that
train on them. Revisit before routing any purpose whose prompt could contain
personal data.

## Candidate discovery is a free catalog fetch, not a benchmark score (owner directive 2026-08-06)

`frontier.run_frontier_research` discovers new OpenRouter candidates by
reading `https://openrouter.ai/api/v1/models` directly (a plain HTTP GET, no
LLM call, no tokens) and keeping the cheapest not-yet-known models. It does
NOT assign any capability score at discovery time — earlier revisions tried
scoring candidates against the Artificial Analysis Intelligence Index (a
general reasoning/coding composite, the wrong instrument for this
finance-specific pipeline) first as fixed tiers, then as a continuous delta;
both were retired. A freshly discovered candidate is capability-neutral
(`promise = 0.5`) until this pipeline's own `model_eval_verdicts` — real
judged output on real production purposes — says otherwise. The transport,
provider-pinning, and eval-gating rules on this page are unchanged; only how
new candidates ENTER the pool got cheaper and more honest about what it does
and doesn't know. See `meta_eval_governance.md` §10.4.

## Relationship to the meta-eval governance design

This backend is the *transport* + the model-identity guardrails. WHICH purposes
to test against WHICH cheap candidates, how to sample real prompts, per-query
criteria, and automated prompt A/B testing are the **meta-eval governance**
design — see `directives/meta_eval_governance.md`. This doc's opt-in candidate
pool is the substrate that design drives.

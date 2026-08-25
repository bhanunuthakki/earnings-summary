# OpenRouter adapter runbook

**Class:** runbook. This file owns OpenRouter setup, upstream identity controls,
data-routing mechanics, and adapter failure diagnosis only. `llm_calls.md` owns live
routing/fallback; `llm_evals.md`, `model_eval_loop.md`, and
`cheapest_model_routing.md` own qualification, promotion, and economics.

## Executable authority

- `src/llm/openrouter_backend.py`: HTTP adapter, key resolution, provider-routing
  object, timeout, usage/cost mapping, and error classification.
- `src/llm/cli.py` and `src/llm/resolver.py`: dispatch and fallback.
- `src/llm/model_ladder.py`: registered family and candidate coordinates.

Model/provider IDs and cost seeds are executable facts. A catalog listing or gateway
model slug is not capability or quality evidence.

## Setup

1. Create and fund the operator account through the provider console.
2. Store the credential as `OPENROUTER_API_KEY` in typed secret configuration. Never
   place it in a URL, CLI argument, log, fixture, or directive.
3. Select any required upstream and data-governance settings in the documented
   environment fields, then run a forced smoke/eval call through the canonical facade.
4. Verify the response, exact runtime coordinates, and attributable ledger row.

Nothing bills this adapter until an explicitly resolved or forced call reaches it.
Setup success does not authorize production routing.

## Stable candidate identity

The gateway can serve one model slug through different upstreams or quantizations. The
adapter therefore sends a provider-routing object on every call:

- upstream fallback is disabled;
- request-parameter support is required;
- data collection defaults to deny;
- a bounded quantization set is declared; and
- `OPENROUTER_PROVIDER_ONLY` can hard-pin an upstream for evaluation/production parity.

The complete candidate Observation Version includes model slug, upstream selection,
quantization, context/runtime parameters, timeout, and data-routing configuration. If
any load-bearing coordinate changes, prior parity evidence does not automatically carry
forward.

## Identity

- **Logical Idempotency Key:** owned by the calling product directive.
- **Content Identity:** prompt/input/output digests and captured response bytes.
- **Observation Version:** the complete gateway/upstream/runtime coordinates plus
  prompt/source versions and provider observation time.
- **Attempt Identity:** `run_id` or unique call receipt for one request; retries change it.

## Failure policy

- Missing/rejected key, permission denial, or exhausted credits: typed setup hard stop.
- Budget denial: hard stop; never route around it here.
- Rate limit, service/network failure, malformed body, unavailable model, or empty
  content: typed operational failure with redacted evidence.
- A forced `backend="openrouter"` comparison fails rather than silently switching
  contestants. Model-routed operational fallback, when implemented, is governed and
  attributed by `llm_calls.md`/`src/llm/cli.py`.
- Gateway upstream fallback remains disabled because an unrecorded upstream change would
  invalidate the evaluated candidate identity.

## Data governance and verification

Prompts traverse both the gateway and selected upstream. Keep the deny-collection default
unless a purpose-specific owner decision permits otherwise; re-review before any purpose
can contain personal or commercially restricted data.

After adapter changes, run focused OpenRouter backend, resolver, ledger, capture,
structured-output, provider-routing, and forced-backend tests. Live evidence is dated and
supplemental; unavailable evidence yields HOLD, not inferred capability.

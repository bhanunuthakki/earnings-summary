# Gemini adapter runbook

**Class:** runbook. This file owns Gemini Developer API setup and adapter-specific
failure diagnosis only. `llm_calls.md` owns live routing and fallback;
`llm_evals.md` and `model_eval_loop.md` own qualification and promotion. Nothing in
this runbook authorizes a production route.

## Executable authority

- `src/llm/gemini_backend.py`: official SDK client, key resolution, model mechanics,
  timeout, usage mapping, ledger writes, and provider-error classification.
- `src/llm/cli.py` and `src/llm/resolver.py`: dispatch and fallback.
- `execution/compare_backends.py` and the model-eval tools: forced comparison calls.

Model IDs and tier aliases live in executable registries. Read them there; do not infer
availability, capability, or current routing from this runbook.

## Setup

1. Create or rotate a Gemini Developer API key using the provider's operator console.
2. Store it in the repo's typed secret environment as `GEMINI_API_KEY` or
   `GOOGLE_API_KEY`. Never pass or print it on a command line.
3. Run the focused adapter smoke/eval command from the repository environment. A valid
   result includes a real response and an attributable `llm_calls` row; a configured
   key alone is not proof of access.

This adapter uses the metered Developer API through the official SDK. It does not depend
on an interactive coding-agent login or inherit agentic tool context.

## Adapter mechanics

- The executable registry selects the requested model and timeout.
- The fast preview alias may self-anneal only through the bounded model-catalog/fallback
  behavior implemented and tested in the adapter. That is adapter availability repair,
  not candidate qualification or a production promotion.
- Response usage metadata is mapped into the shared ledger. Unknown or unavailable cost
  remains explicitly unknown/estimated under the economic policy.
- Capture/replay and forced comparison calls use the same adapter so the evaluated
  runtime configuration matches production mechanics.

## Identity

- **Logical Idempotency Key:** owned by the calling product directive, not this adapter.
- **Content Identity:** prompt/input/output digests recorded by the governed call seam.
- **Observation Version:** requested model/runtime coordinates, adapter configuration,
  prompt/source versions, and provider observation time.
- **Attempt Identity:** `run_id` or unique call receipt for one API attempt; it changes
  on retry.

## Failure policy

- Missing/rejected key or deterministic account/permission failure: typed setup hard
  stop; rotate/fix credentials and rerun the smoke.
- Budget denial: hard stop; do not bypass it through this adapter.
- Timeout, quota, service, malformed response, or unavailable requested model: typed
  operational failure with redacted evidence.
- A forced `backend="gemini"` comparison fails rather than silently switching
  contestants. Model-routed operational fallback, when implemented, is governed and
  attributed by `llm_calls.md`/`src/llm/cli.py`.
- Schema or quality failure is not an adapter fallback decision. Retain the output/error
  and let the eval owner return KEEP/HOLD.

## Verification

Run the focused Gemini backend, resolver, ledger, capture, structured-output, and
forced-backend tests after adapter changes. Live provider evidence is additional and
must be dated; inability to obtain it is reported, never converted into a pass.

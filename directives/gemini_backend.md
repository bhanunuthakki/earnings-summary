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
2. Store it in the repo's typed secret environment (`.env`) as `GEMINI_API_KEY` (or
   `GOOGLE_API_KEY`). If unset in the process environment, `src/llm/gemini_backend.py`
   falls back to `GEMINI_API_KEY` in `AGENT_INSTRUCTIONS_HOME/.env` (or
   `/Applications/agent-instructions/.env`) per `machine-operations.md`. Never pass or
   print it on a command line.
3. Run the focused adapter smoke/eval command from the repository environment. A valid
   result includes a real response and an attributable `llm_calls` row; a configured
   key alone is not proof of access.

This adapter uses the metered Developer API through the official SDK. It does not depend
on an interactive coding-agent login or inherit agentic tool context. Under the current
fleet preference (`gemini > claude`; Codex disabled), `gemini-4-neon` is the driver tier
(`GEMINI_BACKEND_DEFAULT_MODEL`), `gemini-4-hydrogen` is the workhorse and fallback tier
(`GEMINI_BACKEND_FAST_MODEL`, `GEMINI_FALLBACK_MODEL`), and Claude serves as the external
model family and fallback.

## Google AI Studio credits and billing

- **Eligible subscription credits:** Google AI Pro (\$10/month) and AI Ultra (\$40/month
  Ultra 5x, \$100/month Ultra 20x) include monthly Google Developer Program Cloud credits
  (12-month expiration) that apply to external `GEMINI_API_KEY` requests through this
  adapter once claimed onto the linked Cloud Billing account in Google AI Studio (enable
  auto-apply for future grants).
- **What does not apply here:** Daily Google AI Studio web allowances (Playground and Build
  mode) apply only in the AI Studio web UI. Consumer Google One AI credits are a separate
  system and do not apply to Gemini API requests.
- **Prepay and spend guardrails:** On Prepay Cloud Billing accounts (minimum \$5 deposit),
  promotional credits draw before the paid balance, but the paid balance must stay strictly
  above \$0 for promotional credits to apply. Set a monthly project spend cap at
  `https://aistudio.google.com/spend` matching your credit allowance.
- **Safe trial path:** Store the paid key in `.env` as `GEMINI_API_KEY` and evaluate via
  `GEMINI_BACKEND_PURPOSES=<purpose>` or `execution/compare_backends.py`. Never repin
  `LLM_MODELS` without passing `model_eval_loop.md`.

## Gemini 4 prompting and runtime constraints

Cross-project Gemini 4 applicability lives in
`/Applications/agent-instructions/procedures/context-engineering.REFERENCE.md`. Key adapter
and prompt constraints for `gemini-4-neon`, `gemini-4-hydrogen`, `gemini-4-carbon`, and
`gemini-4-argon`:

- **Sampling and thinking controls:** Omit `temperature`, `top_p`, and `top_k` (out-of-range
  values return `400`). Tune depth with `thinking_level` (`low`, `medium`, `high` default,
  `xhigh` on Neon/Hydrogen; `low`, `medium` default, `high` on Carbon/Argon; `minimal` is
  rejected) rather than shrinking `max_output_tokens`. Keep `max_output_tokens` at the
  65,536 synchronous default (or ≥32,768); input, thinking, and output share one 1,048,576-token
  window, and thinking tokens are billed as output tokens. Use `background=True` +
  `max_output_tokens > 65,536` for long decode, and keep HTTP timeouts ≥15 minutes.
- **Caching and prompt order:** Explicit caching (`cachedContents` / `caches.create`) is
  unsupported on Gemini 4. Implicit caching runs automatically on prompts ≥4,096 tokens
  (95% cached-input discount on hits), so place static instructions and shared context at
  the prompt prefix and dynamic inputs at the end.
- **Tools and structured outputs:** Never prompt via system instructions for raw-text JSON
  tool calls when zero tools are declared (`MALFORMED_FUNCTION_CALL`); declare native tools
  or constrain JSON via `response_format`.
- **Deprecations and retirements:** `gemini-2.5-flash` is retired from active fallback
  and candidate selection in favor of `gemini-3.5-flash-lite` (retained in historical
  registries only for ledger and replay compatibility); `gemini-2.5-pro` auto-routes on
  2026-12-01, and `gemini-3.5-flash` and `gemini-3.7-flash` are deprecated.

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

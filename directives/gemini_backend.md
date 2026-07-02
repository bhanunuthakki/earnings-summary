# Gemini second backend (metered API key, eval-gated)

**ROUTING SUPERSEDED 2026-06-13 by `directives/cheapest_model_routing.md`:** Gemini is selected model-first via `family_of()` dispatch; the `GEMINI_BACKEND_ALLOWED_PURPOSES` allowlist is dead code. This doc remains authoritative for backend MECHANICS (API-key auth, self-anneal, corpus/judge harness) and for the 2026-07 migration history below.

**Decision (2026-06-11):** wire Gemini as a SECOND LLM backend behind
`call_llm`. Routing stays Claude-default; a purpose may route to Gemini only
after the LLM-evals judges grade its per-purpose output quality.
Long-context / bulk jobs are the natural eventual fit.

Code: `src/llm/gemini_backend.py` (backend) + `src/llm/cli.py` (routing in
`call_llm`) + `execution/compare_backends.py` (the judging corpus generator).

## 2026-07-01 migration: gemini-cli individuals OAuth was killed by Google

The backend originally shelled out to `gemini-cli` under the consumer
"Login with Google" OAuth path (Gemini Code Assist for individuals),
deliberately **stripping** API keys from the subprocess so every call billed
the flat-rate consumer subscription instead of a metered key. **On
2026-06-18, Google discontinued individual-account OAuth access to
gemini-cli entirely** — every model, every call, unconditionally:

```
IneligibleTierError: This client is no longer supported for Gemini Code
Assist for individuals. To continue using Gemini, please migrate to the
Antigravity suite of products: https://antigravity.google
```

Confirmed live by direct CLI invocation (bypassing our wrapper) across three
different model ids — 100% failure, immediately, at the auth layer, before
any model is even selected. This is **not** version drift, a model-id 404,
or a timeout — upgrading gemini-cli, fixing model ids, or raising
`GEMINI_CLI_TIMEOUT_SECONDS` cannot fix it. Free, Google AI Pro, *and* Ultra
personal accounts are all affected identically; only Workspace/Enterprise
Code Assist licenses or **API-key auth** still work.

**Antigravity CLI (`agy`) / the `google-antigravity` Python SDK — evaluated
and rejected as a replacement.** Both are Google's announced migration path,
and both were installed and tested live before being ruled out:

* `agy --print` is an **autonomous coding-agent harness**, not a completion
  API. Even a trivial prompt ("reply PONG") triggers a multi-step agent loop
  — it reads local permission grants, browses `~/.gemini/procedures/`, does
  live web searches, and writes a "Summary of Work Done" — sometimes
  answering with a clarifying question instead of the requested text. This
  reproduces under `--sandbox` too (that flag restricts filesystem/network
  *scope*, not agentic behavior).
* **No model pinning.** Three different `--model` values (including a
  deliberately bogus one) all silently resolved to the same model. There is
  no way to test `gemini-3.1-pro-preview` vs `gemini-2.5-flash` as distinct
  pareto-optimizer candidates through `agy`.
* **No structured output.** No `--output-format json`, no usage/token/cost
  envelope — confirmed both by direct testing and by the CLI's own upstream
  issue tracker (an `--acp` JSON-RPC mode is an open feature request, not
  shipped).
* The `google-antigravity` Python SDK (`pip install google-antigravity`,
  the `Agent`/`LocalAgentConfig` class) is the same underlying agent
  substrate with no documented way to disable tool use for a bare
  completion, no documented model-pin parameter, and no documented
  usage-metadata fields on its response object. Its own auth options
  collapse to the same metered `GEMINI_API_KEY` or billed Vertex AI ADC —
  i.e. even Google's own "modern" SDK funnels programmatic, model-pinned
  access through the identical metered key this migration ended up using
  directly, but through a heavier, non-deterministic, tool-wielding layer
  on top. Not worth adopting.

**Resolution: call the Gemini Developer API directly** (metered
`GEMINI_API_KEY` / `GOOGLE_API_KEY`), via the same `google.generativeai` SDK
`src/llm/fallback.py` already depended on for its emergency path. This was
never part of the shutdown — Google's own migration guidance states
enterprise/API-key access "remains completely unaffected." Real per-token
cost, not $0: grounded against actual 30-day eval-sweep call volume
(~365 calls across `model_eval` + `backend_judge`, avg ~15-24K char prompts,
~1000-token typical outputs) using `model_ladder.estimated_call_usd`, the
total comes to **~$2/month** — not a meaningful line item next to the AI
Pro/Ultra subscription this repo was paying for anyway, which post-shutdown
buys zero programmatic access. All CLI-specific machinery (subprocess
env-stripping "billing guard", neutral-cwd context-file suppression,
stdin/JSON-envelope parsing) is gone — a bare `generate_content()` call
carries no implicit context by construction, so there was nothing to
isolate.

**Known pre-existing issue, not caused by this migration:** the
`GEMINI_API_KEY` currently in `.env` returns `InvalidArgument: API key not
valid` when tested live. This is why `src/llm/fallback.py`'s emergency path
has sat behind `LLM_FALLBACK_DISABLED=1` since 2026-06 (see that module's
`is_fallback_disabled` docstring) — the operator already knew the key was
bad and chose to silence the noise rather than delete it. The new
`gemini_backend.py` classifies this correctly (`LLMSetupError`, hard stop,
message points at https://aistudio.google.com/app/apikey) but **a real key
rotation is required before any Gemini candidate in the eval sweep can
produce actual data** — until then, calls fail fast and honestly (same
`CANDIDATE_ERRORED` verdict shape as #723) rather than hanging or silently
corrupting results.

## The two Gemini touchpoints

| Touchpoint | Module | Auth / billing | When it runs |
|---|---|---|---|
| **Backend** (this doc) | `src/llm/gemini_backend.py` | `GEMINI_API_KEY` / `GOOGLE_API_KEY` from `.env` — metered API, real list price | Resolved model is Gemini (model-first dispatch), or `backend="gemini"` forced |
| **Fallback** | `src/llm/fallback.py` | Same `GEMINI_API_KEY` — metered API | Only when a CLAUDE call fails operationally |

Both touchpoints now use the identical key and SDK (`google.generativeai`,
deprecated-but-functional — Google's forward path is `google-genai`; see the
`NOTE` comment atop each module). A key rotation fixes both at once. If the
fallback is intentionally silenced (`LLM_FALLBACK_DISABLED=1`), the backend
is NOT affected by that flag — they are independent call sites that happen
to share a credential.

## Setup (operator)

1. Get a Gemini API key at https://aistudio.google.com/app/apikey (or reuse
   the existing one — first check it's actually valid; see the "known
   pre-existing issue" above).
2. Put it in `.env` as `GEMINI_API_KEY=<key>` (or `GOOGLE_API_KEY=`).
3. Verify: `python execution/compare_backends.py --smoke` — the gemini
   column should show real responses with latencies, not `LLMSetupError`.

## Routing & failure policy

Routing is **model-first** — see `directives/cheapest_model_routing.md`, the
authoritative routing doc. `call_llm` resolves a purpose's model (DB
`model_pin_overrides` → `LLM_MODELS` → tier-derived `GEMINI_MODELS`); if the
resolved model is a Gemini id, the call dispatches to this backend via
`family_of()`. `backend="gemini"` forces it (the compare harness). The old
`GEMINI_BACKEND_ALLOWED_PURPOSES` allowlist no longer gates routing — it is dead
code (see the banner above); the resolved model family decides everything.

Failure policy (current): a model-routed Gemini call that fails **operationally**
degrades to Claude, so a model swap can never break the pipeline;
`LLMSetupError` / `LLMBudgetExceeded` propagate per `is_hard_stop`. A **forced**
`backend="gemini"` call raises instead of switching — the caller asked for
Gemini's answer. An invalid/rejected API key raises `LLMSetupError` via
`_classify_gemini_failure` (checks for `Unauthenticated`/`PermissionDenied`,
or `InvalidArgument` carrying an auth-shaped marker like `API_KEY_INVALID` —
a bare `InvalidArgument` without that marker is a genuinely malformed
request, not a key problem, and stays on the operational path).

## Models, cost, ledger

* Model resolution: explicit `GEMINI_MODELS` pin → tier derivation from
  `LLM_MODELS` (Haiku-tier purposes → fast = `gemini-3-flash-preview`, with a
  self-annealing fallback to `gemini-2.5-flash` on `NotFound`
  (#541/#542, re-verified in the 2026-07 API migration); everything else →
  default = `gemini-3.1-pro-preview`). One table drives both backends'
  latency tiers. Bump `GEMINI_BACKEND_FAST_MODEL` / `_DEFAULT_MODEL` when
  Google ships a newer GA tier. The self-anneal discovery source is the
  live API catalog (`genai.list_models()`), not gemini-cli's docs — that
  repo is being retired (see migration history above), so its docs are no
  longer a reliable signal for what a given key can actually call.
* Rate limits are now whatever the Gemini Developer API's free/paid tier
  applies to the configured key (check https://ai.google.dev/gemini-api/docs/rate-limits
  if bulk jobs start getting `ResourceExhausted`) — the old "~60 req/min,
  ~1000/day consumer OAuth" limits no longer apply; this is a different auth
  surface entirely.
* Every call writes the standard `llm_calls` ledger row: `model` starts with
  `gemini-` (with `fallback_used` NULL — fallback rows carry
  `fallback_used='gemini'` instead), token counts mapped from the response's
  `usage_metadata` (`prompt_token_count`→input, `candidates_token_count`→output,
  `cached_content_token_count`→cache-read), and `cost_estimate_usd` = real
  API list price (`model_ladder.estimated_call_usd`, per
  `directives/cheapest_model_routing.md` §1c/§2).
* Timeout: `GEMINI_BACKEND_TIMEOUT_SECONDS` env (default 1200s), mirroring
  `CLAUDE_CLI_TIMEOUT_SECONDS`. Renamed from `GEMINI_CLI_TIMEOUT_SECONDS` in
  the 2026-07 migration (this backend is no longer a CLI wrapper); the old
  name was never set operationally, so the rename is not a breaking change.

## Producing a judging corpus

```
# built-in smoke (viewspec_compile x2, transcript_metadata x1):
python execution/compare_backends.py --smoke

# real purpose, real prompt:
python execution/compare_backends.py --purpose bear_case --prompt-file p.txt --ticker NU
```

Records land in `data/backend_compare/compare_<runid>.jsonl` — one record
per prompt with the full prompt, both responses, models, latencies, errors, and
(for smoke prompts) the expected answer as judge ground truth. Both sides run
through `call_llm` with explicit `backend=` so ledger rows and budget gating
match production behavior exactly.

### Across the whole `--enable-llm` surface: capture + replay

Hand-built prompts (`--smoke`) cover 3 purposes. To compare across **every**
purpose a real build exercises — `bear_case`, `valuation_basis`,
`recent_developments`, `earnings_themes_split`, `qa_topics`,
`exec_comp_alignment`, … — capture the real prompts from a build, then replay
only the Gemini side (the Claude response is reused, so no extra Claude spend):

```
# 1. Harvest real prompts during a real build (capture is OFF unless the env is set).
#    The judge/eval purposes are auto-excluded; LLM_CAPTURE_PURPOSES=csv narrows further.
LLM_CAPTURE_DIR=data/llm_capture python execution/build_artifacts.py --enable-llm \
    --ticker NU --repo-root <MAIN repo>

# 2. Replay each captured Claude prompt through Gemini ONLY, into a compare corpus.
python execution/compare_backends.py --from-capture data/llm_capture/capture_<date>.jsonl \
    --repo-root <MAIN repo>

# 3. Grade it (dual judge), exactly as any other corpus.
python execution/grade_backends.py --repo-root <MAIN repo>
```

The capture sink is `src/llm/capture.py` (env-gated, off by default, best-effort —
prompts embed thesis/IR content so the ledger stays sha-only; this is the opt-in
full-text path). Records dedup by `prompt_sha256` on replay. **Web purposes**
(`recent_developments`, scope `web`) are captured + flagged but are structurally
Claude-favored — the Gemini backend has no web tools, so a low Gemini score there
means "can't do this purpose without web", not "worse model". Filter them out
(`--purpose`) or read the `captured_scope` field when interpreting the verdict.

## Grading the corpus (the promotion verdict)

`src/llm/backend_judge.py` + `execution/grade_backends.py` turn a corpus into a
per-purpose recommendation. This is **pairwise** (is Gemini at parity with Claude
*for this purpose*?) — distinct from the general LLM-evals harness, which scores
one model's output absolutely against a golden set / rubric.

```
# grade the most recent corpus with BOTH judges (Claude Opus + Gemini Pro):
python execution/grade_backends.py --repo-root <MAIN repo>

# one corpus, one purpose, Claude judge only:
python execution/grade_backends.py --run-id c64bbd98 --purpose viewspec_compile --judges claude
```

How it stays honest:
* **Brand-blind.** The judge sees "Response A" / "Response B", never which model
  wrote which — judges carry brand priors.
* **Position-swap.** Every pair is judged twice with the sides swapped; a backend
  wins only if both passes agree once mapped back. A flip ⇒ a non-robust tie
  (`position_consistent=false`) — the judge was following position, not quality.
* **Dual judge.** `--judges claude,gemini` grades with Claude Opus AND Gemini Pro;
  cross-judge agreement is the headline (cancels same-family favouritism). The
  Gemini judge uses a forced `backend="gemini"` — legitimately bypassing the
  production allowlist, because a judge call is not production routing.
* **Fail closed.** An unparseable / failed judge verdict never passes a side; the
  pair resolves to a tie with the raw text preserved (mirrors `src/evals/judge.py`).

Judge calls bill under `purpose="backend_compare_judge"` (Opus pin in `LLM_MODELS`;
Gemini side → Pro), sharing the grade `run_id` so cost joins from `llm_calls`.
Output: `data/backend_compare/graded_<runid>.jsonl` (one line per judged pair) +
`summary_<runid>.json` (rollups + agreement). The recommendation
(`PROMOTE_CANDIDATE` / `HOLD` / `REJECT` / `INSUFFICIENT_DATA`) is **advisory** —
it's the evidence you cite in the PR that edits `GEMINI_BACKEND_ALLOWED_PURPOSES`,
never an automatic gate. A pre-login corpus (Gemini side failed) grades to
all-skips with a clear message.

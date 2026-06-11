# Gemini second backend (consumer subscription, eval-gated)

**Decision (2026-06-11):** wire consumer-subscription Gemini as a SECOND LLM
backend behind `call_llm`. Routing stays Claude-default; a purpose may route to
Gemini only after the LLM-evals judges grade its per-purpose output quality.
Long-context / bulk jobs are the natural eventual fit.

Code: `src/llm/gemini_backend.py` (backend) + `src/llm/cli.py` (routing in
`call_llm`) + `execution/compare_backends.py` (the judging corpus generator).

## The three Gemini touchpoints — don't confuse them

| Touchpoint | Module | Auth / billing | When it runs |
|---|---|---|---|
| **Backend** (this doc) | `src/llm/gemini_backend.py` | Consumer OAuth (`gemini` CLI login) — $0 marginal | Purpose in the eval-gated allowlist, or `backend="gemini"` forced |
| **Fallback** | `src/llm/fallback.py` | `GEMINI_API_KEY` from `.env` — metered API | Only when a CLAUDE call fails operationally |
| **`.env` key** | `GEMINI_API_KEY=` | feeds the fallback ONLY | Never reaches the backend (stripped from its subprocess env) |

## One-time setup (operator)

1. `npm install -g @google/gemini-cli` (installed 2026-06-11, v0.46.0 — the
   binary is `C:\Users\bhanu\AppData\Roaming\npm\gemini.cmd`).
2. **Log in once, interactively:** run `gemini` in any terminal, pick
   **Login with Google**, complete the browser flow. Credentials cache under
   `C:\Users\bhanu\.gemini\` and renew themselves. Until this is done, every
   backend call fails fast with `LLMSetupError` carrying this exact hint
   (verified: headless auth failures exit 41 in ~6s — no hang, no browser).
3. Verify: `python execution/compare_backends.py --smoke` — the gemini column
   should flip from `LLMSetupError` to real responses with latencies.

## Billing guard (the no-silent-API-key invariant)

The backend subprocess **cannot** bill the metered API, by construction —
mirroring how the canonical claude wrapper guards `ANTHROPIC_API_KEY`:

* `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`,
  `GOOGLE_GENAI_USE_VERTEXAI` are **stripped** from the subprocess env
  (the repo's `.env` key stays available to the fallback path only).
* `GOOGLE_GENAI_USE_GCA=true` **forces** the consumer OAuth (Gemini Code
  Assist) auth path. No cached login ⇒ fail fast, never key-fallback.
* `GEMINI_CLI_TRUST_WORKSPACE=true` — headless runs in the backend's neutral
  cwd are untrusted by default (v0.46 trust gate), which would both block the
  run and ignore workspace settings. The neutral cwd is an empty temp dir we
  create, so trusting it is safe.
* `NO_BROWSER=true` — belt-and-braces against any future interactive-auth
  attempt from a cron box.

Guarded subprocess mechanics (same Windows gotchas as the claude wrapper):
absolute `gemini.cmd` path via `shutil.which` (PATHEXT isn't applied to bare
names), forced UTF-8 with `errors="replace"`, prompt via **stdin** (no `-p`;
piped stdin enters headless mode and dodges the 32K CreateProcess limit),
`-o json` envelope parsed for the response + token stats.

**Context isolation:** the neutral cwd carries a `.gemini/settings.json`
pointing `context.fileName` at a filename that exists nowhere, so neither the
repo's project context nor the user's global `~/.gemini/GEMINI.md` rulebook
(~17KB) is injected into backend prompts. Without this every judged output
would be contaminated by machine-setup instructions.

## Routing policy (the eval gate)

```
call_llm(prompt, purpose=...)            -> Claude (always, today)
call_llm(..., backend="gemini")          -> Gemini, forced (compare harness)
call_llm(purpose in allowlist)           -> Gemini, once judges pass it
```

* `GEMINI_BACKEND_ALLOWED_PURPOSES` (in `gemini_backend.py`) **ships EMPTY**.
  Adding a purpose requires: a `compare_backends` corpus for that purpose →
  the evals-track judges grade Gemini vs Claude on it → the PR adding the
  purpose links the verdict. `tests/test_gemini_backend.py::
  test_allowlist_ships_empty` enforces the empty default; it is updated in
  the same PR that passes an eval.
* `GEMINI_BACKEND_PURPOSES` env var (comma-separated) merges extra purposes
  for the current process — the local-trial escape hatch. Never set it in
  cron/task definitions.
* An explicit `model=` pin with no explicit backend always stays on Claude.
* Failure policy: allowlist-routed Gemini calls that fail **operationally**
  degrade to Claude (enabling a purpose can never break the pipeline);
  `LLMSetupError` / `LLMBudgetExceeded` propagate per `is_hard_stop`. A
  **forced** `backend="gemini"` call raises instead of switching — the
  caller asked for Gemini's answer.

## Models, cost, ledger

* Model resolution: explicit `GEMINI_MODELS` pin → tier derivation from
  `LLM_MODELS` (Haiku-tier purposes → `gemini-3.5-flash` (GA 2026-05-19),
  everything else → `gemini-2.5-pro`). One table drives both backends' latency
  tiers. Bump `GEMINI_BACKEND_FAST_MODEL` / `_DEFAULT_MODEL` when Google ships a
  newer GA tier.
* Consumer-tier limits (Login with Google, free individual plan): ~60
  requests/min, ~1000/day, models can transparently reroute pro→flash under
  load; a paid Google AI plan raises the caps. Check the plan if bulk jobs
  start rate-limiting.
* Every call writes the standard `llm_calls` ledger row: `model` starts with
  `gemini-` (with `fallback_used` NULL — fallback rows carry
  `fallback_used='gemini'` instead), token counts mapped from the CLI's
  session stats (`prompt`→input, `candidates`→output, `cached`→cache-read),
  and `cost_estimate_usd=0.0` — an explicit zero meaning *measured: free
  under the subscription*, so budget sums stay correct.
* Timeout: `GEMINI_CLI_TIMEOUT_SECONDS` env (default 1200s), mirroring
  `CLAUDE_CLI_TIMEOUT_SECONDS`.

## Producing a judging corpus

```
# built-in smoke (viewspec_compile x2, transcript_metadata x1):
python execution/compare_backends.py --smoke

# real purpose, real prompt:
python execution/compare_backends.py --purpose bear_case --prompt-file p.txt --ticker NU
```

Records land in `data/backend_compare/compare_<runid>.jsonl` — one record per
prompt with the full prompt, both responses, models, latencies, errors, and
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

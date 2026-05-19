# Directive: LLM Calls

## Goal

Every LLM call in this repo goes through ONE entry point so retunes (model
swap, timeout change, billing change, fallback policy) happen in one place
and never silently diverge per script.

## The canonical entry point

```python
from llm_client import call_llm

response = call_llm(prompt, purpose="bear_case")
```

`call_llm(prompt, *, purpose=None, model=None, timeout_seconds=None)` lives in
`src/llm_client.py`. It:

1. Resolves `purpose` to a Claude model id via `LLM_MODELS` (or uses an
   explicit `model` arg as escape hatch).
2. Calls the Claude Code CLI subprocess. The CLI honors whatever auth is
   configured in the environment — `ANTHROPIC_API_KEY` for metered API
   billing, or `claude auth login` for subscription billing — operator's
   choice.
3. On any operational failure (timeout / non-zero exit / empty stdout / binary
   missing mid-run), **automatically** falls back to Gemini Flash if a
   `GEMINI_API_KEY` is configured. No per-call wiring needed.
4. Setup errors (`claude` binary missing on first call) raise loudly without
   engaging the fallback — those are operator problems, not retry-able
   failures.

## Hard rules

1. **Direct provider SDKs are forbidden outside `src/llm_client.py`.** No
   `import google.generativeai` in `execution/`, `src/report/sections/`, or
   anywhere else. No `import anthropic`. The fallback wiring inside
   `llm_client._try_gemini_fallback` is the ONLY place Gemini is touched.
2. **Every `call_llm` invocation MUST pass `purpose="..."`.** Anonymous calls
   default to `DEFAULT_MODEL` with a warning log; that warning means a new
   purpose key needs registering, not silenced.
3. **Per-section model selection lives in `LLM_MODELS` only.** Don't pass
   `model="claude-..."` ad-hoc at call sites. If a section needs a different
   model, add or update its entry in `LLM_MODELS` so the choice is reviewable.
4. **No `genai.GenerativeModel(...)` retries, no parallel `_try_*` helpers.**
   The Claude→Gemini cascade lives in `_call_claude` and is the only retry
   logic. Single source of truth.

## Adding an LLM-backed section

1. Pick a purpose key (e.g., `"saydo_extraction"`). Use `snake_case`.
2. Add it to `LLM_MODELS` in `src/llm_client.py` with a model id (`DEFAULT_MODEL`
   for analytical writing, `FAST_CLASSIFIER_MODEL` for short structured calls)
   and a one-line comment on rationale.
3. In your section / script, write the prompt as a module-level constant
   (greppable, reviewable) and call:
   ```python
   from llm_client import call_llm
   raw = call_llm(my_prompt, purpose="saydo_extraction")
   ```
4. Strip JSON fences if your prompt expects strict JSON — the Claude CLI
   sometimes wraps despite instruction. Use the `JSON_FENCE_RX` pattern
   that's already established in the extractors.

## Failure modes you don't have to handle

- Claude CLI missing → `_verify_setup_once` raises with install instructions.
- Claude CLI timeout / empty output → automatic Gemini fallback if key set.
- Both Claude and Gemini fail → `_try_gemini_fallback` re-raises the original
  Claude error chained with the Gemini error so both surface together.

## Failure modes you DO have to handle

- Caller-side prompt errors (no input, prompt too long for the chosen model).
- Caller-side response parsing (the LLM didn't follow your output format).
- JSON-fence wrapping when you asked for strict JSON.

## Migration history

This directive supersedes the inconsistent state where scripts called
`google.generativeai` directly:

- `execution/extract_nvo_patent_timeline.py` — migrated 2026-05-09

Any future script that goes around `call_llm` is a regression and should be
caught in code review.

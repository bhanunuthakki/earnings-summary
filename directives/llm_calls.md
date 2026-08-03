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

`call_llm(prompt, *, purpose=None, model=None, timeout_seconds=None)` is
implemented in `src/llm/cli.py` and re-exported by `src/llm_client.py`. It:

1. Resolves `purpose` to the existing eval-gated quality tier in `LLM_MODELS`
   (or uses an explicit `model` arg as an escape hatch).
2. For normal purpose-resolved traffic, calls the isolated Codex membership
   transport first: Haiku-class purposes map to `gpt-5.6-luna`,
   Sonnet-class purposes to `gpt-5.6-terra`, and Opus/Fable-class purposes to
   `gpt-5.6-sol`.
3. On an operational Codex failure, falls back to the Claude subscription
   transport and records `fallback_used='claude'`. The existing Claude
   operational fallback remains available after that where configured.
4. Explicit provider-family requests remain explicit: a caller passing a
   Claude, Gemini, or OpenRouter model id routes to that family rather than
   being silently translated.
5. `LLM_PRIMARY_SUBSCRIPTION_BACKEND=claude` is the reversible rollback
   switch. The production default is `codex`.
6. `call_llm_with_web` follows the SAME Codex-first order (2026-08-03 owner
   ratification: "everything routed to Codex first, Claude is backup" —
   including web-grounded calls). The Codex membership wrapper now supports
   an opt-in `web_search` mode (`disabled` default / `cached` / `indexed` /
   `live`); `call_llm_with_web`'s Codex leg passes `web_search="live"` so it
   can fetch fresh pages, falling back to the existing Claude
   WebSearch/WebFetch tool-call path on an OPERATIONAL Codex failure only —
   never as a routing preference. Every other Codex call site keeps the
   `"disabled"` default (byte-identical to before this changed).
   **Budget-cap nuance**: the Claude web leg's hard per-call
   `--max-budget-usd` ceiling (`CLAUDE_WEB_MAX_BUDGET_USD`, $2) is a
   Claude-CLI-only mechanism — Codex is membership-billed with no per-call
   price and the wrapper reports no token usage, so there is no dollar
   figure to clamp on the Codex leg. That is a deliberate gap, not a
   silently dropped concept: the per-purpose MONTHLY budget
   (`_enforce_budget_pre_call`) still gates every leg, Codex included,
   identically, and the Codex wrapper's own isolation (read-only sandbox, no
   shell/apps/hooks) bounds a runaway call's blast radius even without a
   $-ceiling.

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
   Ordered transport fallback lives in `call_llm` / `_call_claude` and is the
   only retry logic. Single source of truth.

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

- Codex membership transport unavailable → a structured warning and Claude
  fallback, with both attempts separately ledgered.
- Claude CLI timeout / empty output after Codex fallback → the existing
  configured fallback policy applies.
- Explicit/forced backend failure → raises; forced-family comparisons never
  silently switch contestants.

## Failure modes you DO have to handle

- Caller-side prompt errors (no input, prompt too long for the chosen model).
- Caller-side response parsing (the LLM didn't follow your output format).
- JSON-fence wrapping when you asked for strict JSON.

## Structured output (JSON-expecting calls)

New call sites that expect JSON should route through
`llm.structured.call_llm_structured(prompt, purpose=..., expect="object"|"array",
required_keys=(...))` instead of hand-rolling fence-strip + `json.loads` +
`except`. It gives you the proven retry-with-feedback (one re-ask telling the
model its previous response wasn't valid JSON) and it is LOUD on final
failure (`StructuredParseError`) — never return a `{}`/`[]`/`None` that is
indistinguishable from a real "nothing found" (the silent-empty pathology,
llm_evals_plan §5.4; `risk_factor_classify` used to persist fabricated
"other" categories this way). Existing sites migrate opportunistically.

## Prompt-change workflow (the regression gate)

Materially rewriting a prompt is a measurable change, not a vibe
(directives/llm_evals_plan.md §2.4). The loop:

1. Edit the prompt.
2. Bump its entry in `src/llm/prompt_versions.py` (`"v1"` → `"v2"`);
   unregistered purposes get registered on first bump.
3. Run its eval against the production DB:
   `python execution/run_llm_evals.py --purpose <p> --min-score 0.8
   --repo-root <MAIN repo>` — exit 3 means the rewrite regressed below the
   bar; don't merge it. (Which purposes have eval coverage:
   `python execution/run_llm_evals.py --coverage`.)
4. Compare versions: the System → Evals panel's "Score by prompt version"
   strip (or `summarize_by_prompt_version`) shows v2 vs v1 side by side.
   First real win of this loop: viewspec_compile v2 scored 16/16 vs v1's
   13/16 (#427).

The gate is a pre-merge MANUAL step — LLM calls in CI are forbidden /
monkeypatched by design.

## Migration history

This directive supersedes the inconsistent state where scripts called
`google.generativeai` directly:

- `execution/extract_nvo_patent_timeline.py` — migrated 2026-05-09

Any future script that goes around `call_llm` is a regression and should be
caught in code review.

## Prompt-A/B promotion workflow (meta_eval_governance.md §4 + §10 Q1)

Prompt improvements are MEASURED, then AUTO-APPLIED (owner decision Q1) —
never hand-tuned in place:

1. **Propose** — `python execution/run_prompt_ab.py --purpose <p> --propose
   --template-file <the checked-in constant's text>`. The Opus proposer emits
   1-4 exact-match edits on the instruction scaffold; anchors are validated
   against the template AND real captured renders BEFORE any spend
   (`rejected_anchor` otherwise).
2. **Run** — `--experiment <id>` (≥2 runs on fresh samples). Baseline reuses
   captured incumbent outputs when the capture's model equals the frozen model;
   the variant runs under the SAME frozen model, `scope="prompt_ab"`, capture
   OFF. The brand-blind judge grades baseline (slot A) vs variant (slot B) with
   §3 criteria derived from the BASELINE prompt; judges never see the edits.
3. **Promote** — `--experiment <id> --promote`: if the pooled §4.4 bar holds
   (≥60% strict variant wins AND ≤20% baseline wins per judge, agreement ≥0.6,
   ≥2 promoting runs over ≥10 pooled cases, zero KEEP_BASELINE runs), an ACTIVE
   `prompt_pin_overrides` row applies the edits to PRODUCTION traffic at
   `call_llm`/`call_llm_with_web` time (production scopes only — replays stay
   byte-identical; anchor drift fails OPEN to the original prompt).
4. **Reconcile git** — the override's `reason_json.edits` carries the exact
   diff. Catch the checked-in prompt constant up in a routine PR and bump its
   `prompt_versions` entry (v2→v3 …); once the constant matches, the override
   is redundant and can be deactivated.
5. **Auto-demote** — a later experiment run concluding KEEP_BASELINE
   deactivates the override automatically (mirrors the model loop's regression
   revert). Manual revert: deactivate the row (history is kept).

Rule-3 kinship: an override never bypasses `_model_for` or the ledger — the
edited prompt is THE production prompt (its sha, capture, and cost accounting
all follow it).

# Directive: cheapest-at-parity model routing (unified, incl. Gemini)

**Status: SHIPPED 2026-06-13** — Chip 2 (PRs #533/#536/#537/#538) + fast-model hardening (#541/#542). This is the LIVE cost-aware routing design and the most-active theme. PR D promoted the FIRST 5 classifier purposes; further promotions remain eval-gated/open.

**Goal:** route every LLM purpose to the cheapest model that holds parity with
the incumbent — across ALL model tiers and BOTH backends (Claude and Gemini).
Gemini is a first-class candidate, not a special-cased second backend: a Gemini
model ID in `LLM_MODELS` or `model_pin_overrides` is enough to route a purpose
there. The eval-gated path selects cheapest-at-parity using public API pricing
as the cost basis, and includes token efficiency (output token count) alongside
output quality in every verdict.

---

## 1. Corrected production routing (three changes needed)

### 1a. `call_llm` — model-first, then backend from family

**Current (broken for cross-backend):**
```
call_llm(purpose=P, model=None, backend=None)
  → check allowlist (GEMINI_BACKEND_ALLOWED_PURPOSES, always empty) → "claude"
  → _model_for(P) → returns any model id (incl. gemini-*)
  → _call_claude(model=gemini-3.1-pro-preview)  ← WRONG: Claude CLI rejects it
```

**Corrected flow:**
```
call_llm(purpose=P, model=None, backend=None)
  → resolved_model = _model_for(P)     ← DB pin OR LLM_MODELS[P] OR DEFAULT_MODEL
  → resolved_backend = backend or (GEMINI if family_of(resolved_model)==GEMINI else CLAUDE)
  → if GEMINI  → call_gemini(prompt, model=resolved_model, ...)
  → if CLAUDE  → _call_claude(prompt, model=resolved_model, ...)
```

The key invariant: `_model_for()` already reads `model_pin_overrides` and
`LLM_MODELS`. When either table holds a Gemini model ID for a purpose, the
backend auto-follows. No separate allowlist needed.

**Explicit `backend=` override still works** for the compare harness and ad-hoc
debugging: `call_llm(..., backend="gemini")` forces Gemini regardless of the
resolved model. This path should remain as-is.

**Diff (conceptual) in `call_llm`:**
```python
# REMOVE this block:
resolved_backend = backend
if resolved_backend is None:
    from llm.gemini_backend import gemini_allowed_purposes
    is_allowlisted = (model is None and purpose is not None
                      and purpose in gemini_allowed_purposes())
    resolved_backend = "gemini" if is_allowlisted else "claude"

# REMOVE this block (model resolution happened AFTER backend in the old flow):
if model is None:
    resolved_model = _model_for(purpose)
else:
    resolved_model = model

# REPLACE WITH:
from llm.model_ladder import family_of, GEMINI as _GEMINI_FAMILY  # late import
if model is None:
    resolved_model = _model_for(purpose) if purpose is not None else DEFAULT_MODEL
else:
    resolved_model = model
resolved_backend = backend or (_GEMINI_FAMILY if family_of(resolved_model) == _GEMINI_FAMILY
                               else "claude")
```

The Gemini try/except (operational fall-through to Claude) is preserved
unchanged — the fail-closed behavior is still correct.

### 1b. `GEMINI_BACKEND_ALLOWED_PURPOSES` — deprecated

Once the routing above ships, the allowlist in `gemini_backend.py` is dead code.
`gemini_allowed_purposes()` is still called from one place in the old `call_llm`
(which goes away). Deprecate the symbol and remove it in the same PR that lands
the routing fix.

Keep `GEMINI_BACKEND_PURPOSES` env var as a debug escape hatch if needed — but
it now just means "run these with Gemini" rather than "allow them through the
allowlist gate", which is semantically the same once the routing is model-first.

### 1c. `gemini_backend.py` — remove the $0 cost pin

`usage_meta_from_gemini_stats` currently pins `"total_cost_usd": 0.0`. Replace
with a real cost estimate using `model_ladder.estimated_call_usd`:

```python
cost = estimated_call_usd(resolved_model, prompt_tokens, candidate_tokens)
return {
    "usage": {
        "input_tokens": prompt_tokens,
        "output_tokens": candidate_tokens,
        "cache_read_input_tokens": cached_tokens,
    },
    "total_cost_usd": cost,   # real API price, not $0
}
```

---

## 2. Cost basis — public API prices, not subscription flat rate

Both Claude and Gemini are accessed via flat-rate consumer subscriptions, but
that makes all marginal costs look like $0 — useless for choosing which model is
cheapest-at-parity. Use **public list API prices** so the model ladder ranks
correctly and `cost_estimate_usd` in `llm_calls` carries a meaningful signal.

Update `src/llm/model_ladder.py`:
- Remove `subscription=True, rate_limited=True` from Gemini entries
- Set real `input_usd_per_mtok` / `output_usd_per_mtok` from the official pricing
  pages (verify at `ai.google.dev/pricing` and `anthropic.com/pricing` before
  merging — these change):

| Model id (in code) | Public equivalent | Approx. in $/MTok | Approx. out $/MTok |
|---|---|---|---|
| `claude-haiku-4-5-20251001` | Claude Haiku 4.5 | $0.80 | $4.00 |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 | $3.00 | $15.00 |
| `claude-opus-4-7` | Claude Opus 4.7 | $15.00 | $75.00 |
| `claude-opus-4-8` | Claude Opus 4.8 | $15.00 | $75.00 |
| `gemini-2.5-flash` | Gemini 2.5 Flash | $0.30 | $2.50 |
| `gemini-3.1-pro-preview` | Gemini 2.5 Pro | $1.25 | $10.00 |

**Verify these before merging.** The model IDs used by the Gemini CLI may differ
from the REST API IDs — check `gemini --help` or the CLI changelog for the
canonical mapping.

**FAST tier is `gemini-3-flash-preview` (not hand-pinned in `LLM_MODELS`).** The
backend's fast tier resolves to `gemini-3-flash-preview` with a self-annealing
discovery on `ModelNotFoundError` (falls back to `gemini-2.5-flash`, #541/#542).
Callers should NOT pin a Flash id in `LLM_MODELS` — let the backend resolve and
anneal it; the `gemini-2.5-flash` id above is the price-table/fallback anchor.

The `blended_usd_per_mtok` property remains unchanged (6:1 input:output weight);
`estimated_call_usd` likewise unchanged. The only edit is the price values and
removal of the `subscription` flag.

With real prices, the ladder order becomes:
```
Gemini 2.5 Flash ($0.57 blended) < Haiku 4.5 ($1.26) < Gemini 2.5 Pro ($2.50)
  < Sonnet 4.6 ($4.71) < Opus 4.7/4.8 ($18.21)
```
This is the correct search order for `cheaper_candidates()`.

---

## 3. Token efficiency — add to eval recording

A model that produces the same quality output in fewer tokens is strictly better:
it's cheaper (real API price × tokens) and faster. This should be measured per
eval case and surfaced in the verdict.

### 3a. `ModelRunResult` — add token count

```python
@dataclass(frozen=True, slots=True)
class ModelRunResult:
    model_id: str
    ok: bool
    response: str
    error: str | None
    elapsed_ms: int
    output_tokens: int = 0      # ← NEW: from the ledger row written by run_model
    input_tokens: int = 0       # ← NEW
```

`run_model()` already calls `call_llm()` which writes a `llm_calls` row. After
the call, read back the ledger row by `run_id` to get the token counts (or have
`call_llm` return them — whichever is less invasive).

Simplest low-friction path: after `call_llm`, query `llm_calls` for the most
recent row matching `(run_id, purpose, scope="model_eval")` and extract
`input_tokens`/`output_tokens`. This is a best-effort read that does not block
the eval if the ledger is unavailable.

### 3b. `CandidateVerdict` — add token efficiency fields

```python
@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    purpose: str
    incumbent: str
    candidate: str
    n: int
    candidate_wins: int
    incumbent_wins: int
    ties: int
    parity_rate: float
    judge_agreement: float
    recommendation: str
    reason: str
    # ← NEW token efficiency fields (mean over all n cases):
    candidate_output_tokens_mean: float = 0.0
    incumbent_output_tokens_mean: float = 0.0
    token_efficiency_ratio: float = 1.0  # candidate / incumbent output tokens
```

`token_efficiency_ratio < 1.0` = candidate more concise (usually good).
`token_efficiency_ratio > 1.5` = candidate significantly more verbose (flag it).

### 3c. `model_eval_verdicts` — store per-case token data in `summary_json`

`summary_json` already holds the full verdict + per-case judge rationales.
Extend the per-case audit dict in `run_model_eval_sweep._evaluate_candidate`:

```python
case_audit.append({
    "label": jp.label,
    "judge": jb,
    "winner_model": winner_model,
    "margin": jp.margin,
    "position_consistent": jp.position_consistent,
    "rationales": jp.rationales,
    "candidate_output_tokens": cand.output_tokens,   # ← NEW
    "incumbent_output_tokens": case.incumbent_output_tokens,  # ← NEW (add to PromptCase)
})
```

And at the verdict level:
```python
summary_json = json.dumps({
    "verdict": dataclasses.asdict(verdict),   # now includes token_efficiency_ratio
    "cases": case_audit,
}, ...)
```

No schema migration needed — `summary_json` is already TEXT.

### 3d. How token efficiency affects the switch decision

Token efficiency is a **secondary signal**, not a gate:
- Primary gate: `parity_rate >= parity_threshold` (quality)
- Secondary context: `token_efficiency_ratio` in the verdict log
- If `token_efficiency_ratio > 1.5` (candidate uses 50% more output tokens), flag
  it as a cost headwind in the recommendation reason even when quality holds.
- If `token_efficiency_ratio < 0.8` (candidate is 20% more concise), note it as
  an additional cost saving beyond the model price difference.

Do NOT use token_efficiency_ratio as a hard gate for SWITCH_DOWN — a model that
uses slightly more tokens but costs $0.30/MTok vs $3.00/MTok still wins on cost.
The real cost comparison is `estimated_call_usd(candidate, tokens_candidate)` vs
`estimated_call_usd(incumbent, tokens_incumbent)` — with real prices both token
count and per-token price matter.

---

## 4. Cost table (19 days: 2026-05-25 → 2026-06-13)

| Purpose | Model | Calls | Cost (19d) | Ann. est.* | Gemini candidate? |
|---|---|---|---|---|---|
| `bear_case` | Sonnet | 166 | $97.84 | ~$1,880 | Gemini 2.5 Pro — early run REJECTED 4/4 |
| `recent_developments` | Sonnet+web | 194 | $69.80 | ~$1,340 | NO (needs web tools) |
| `earnings_themes_split` | Sonnet | 156 | $55.02 | ~$1,057 | Gemini 2.5 Pro — eval needed |
| `qa_topics` | Sonnet | 400 | $22.65 | ~$435 | Gemini 2.5 Pro — eval needed |
| `exec_comp_alignment` | Opus | 145 | $18.97 | ~$365 | not in LLM_MODELS — investigate |
| `saydo_filter` | Sonnet | 259 | $18.51 | ~$356 | Gemini 2.5 Pro — eval needed |
| `pairwise_analysis` | Sonnet | 39 | $13.13 | ~$252 | Gemini 2.5 Pro — eval needed |
| `company_description` | Opus/Sonnet | 50 | $13.20 | ~$254 | Gemini 2.5 Pro — eval needed |
| `valuation_basis` | Opus | 122 | $11.76 | ~$226 | RISKY (sector judgment) |
| `news_structuring` | Opus | 20 | $11.60 | ~$223 | Gemini 2.5 Pro — golden file exists |
| `canonicalize_segments` | Haiku | 70 | $4.19 | ~$80 | Gemini Flash — eval needed |
| `kpi_registry_auto_proposal` | Opus | 22 | $4.16 | ~$80 | RISKY (drives alert thresholds) |
| Fast-classifier cluster | Haiku | ~130 | ~$2.2 | ~$42 | Gemini Flash — golden sets |

*Annualised from 19 days; period was high-activity (S11/S12 PRs), steady-state lower.
**Total (19d): $400.19 · Ann. est.: ~$7,700.**

Note: `exec_comp_alignment` (Opus, 145 calls, $18.97) is NOT in `LLM_MODELS` —
it fell through to `DEFAULT_MODEL` (Sonnet) but somehow hit Opus. Likely has a
`model_pin_overrides` row or an explicit `model=` at the call site. Investigate
before including in the eval sweep.

**Conservative addressable opportunity** (Gemini 2.5 Pro at parity for
`earnings_themes_split` + `qa_topics` + `saydo_filter` + `pairwise_analysis` +
`company_description` + `news_structuring`): at real API prices
(Sonnet $3/$15 → Gemini Pro $1.25/$10), rough savings ~55–65% per call.
$133.11 × 0.60 = **~$80 per 19 days → ~$1,530/year** from those six purposes.
Haiku cluster → Flash: ~$2.2 × 0.85 = ~$1.87 per 19d (~$36/yr, smaller but free).

---

## 5. Safe vs risky candidate split

### SAFE — deterministic eval; cert by running the golden-set grader

| Purpose | Incumbent | Gemini target | Eval |
|---|---|---|---|
| `viewspec_compile` | Haiku | gemini-2.5-flash | golden set (valid ViewSpec JSON) |
| `transcript_metadata` | Haiku | gemini-2.5-flash | golden set (ticker_Q_YYYY) |
| `intake_classifier` | Haiku | gemini-2.5-flash | golden set (doc-type enum) |
| `decision_conditions_extract` | Haiku | gemini-2.5-flash | golden set (JSON schema) |
| `ask_pack_router` | Haiku | gemini-2.5-flash | golden set (closed enum) |
| `peer_selection` | Sonnet | gemini-3.1-pro-preview | golden set; sibling chip running |
| `podcast_takeaway_summary` | Sonnet | gemini-3.1-pro-preview | golden set |
| `news_structuring` | Opus | gemini-3.1-pro-preview | golden file exists (wire into GOLDEN_PURPOSES) |

### CANDIDATE — high cost, needs eval harness first

| Purpose | Incumbent | Ann. est. | Blocker |
|---|---|---|---|
| `earnings_themes_split` | Sonnet | ~$1,057 | build rubric AuditSpec |
| `qa_topics` | Sonnet | ~$435 | build rubric AuditSpec |
| `saydo_filter` | Sonnet | ~$356 | golden set feasible (filter accuracy) |
| `pairwise_analysis` | Sonnet | ~$252 | rubric or golden set |
| `company_description` | Sonnet | ~$254 | rubric AuditSpec |
| `canonicalize_segments` | Haiku | ~$80 | golden set feasible (structured output) |

### RISKY / NEVER — keep on Claude unless very strongly certified (n≥10, both-judge, margin>0.5)

| Purpose | Reason |
|---|---|
| `bear_case` | Gemini REJECTED 4/4; adversarial analytical reasoning |
| `recent_developments` | Needs web tools; Gemini backend structurally disadvantaged |
| `valuation_basis` | Sector judgment; wrong multiple = silent harm per ticker |
| `material_news_classification` | Alert veto; false-negative = missed alert |
| `kpi_registry_auto_proposal` | Drives alert thresholds; Opus instruction-following |
| `earnings_tone_diff` | Alert trigger; high-stakes |
| `advisor_*` | Portfolio advice; Opus judgment tier |

---

## 6. Eval changes (the sweep with the three corrections applied)

With real prices and token efficiency wired in, a single weekly sweep covers
both Claude-tier downgrades AND Gemini candidates in one pass:

```
purpose → _model_for(P) → incumbent model (e.g. claude-sonnet-4-6)
cheaper_candidates(incumbent) → [gemini-2.5-flash, claude-haiku-4-5, gemini-3.1-pro-preview]
for each candidate:
    run_model(candidate) → response + output_tokens
    judge(incumbent_response vs candidate_response) → quality verdict
    compare cost: estimated_call_usd(candidate, cand_tokens) vs estimated_call_usd(incumbent, inc_tokens)
    record: CandidateVerdict (parity_rate, token_efficiency_ratio, both costs)
if SWITCH_DOWN:
    apply_model_switches → write_pin_override(purpose, candidate)
    next call_llm(purpose): _model_for(P) → candidate (could be gemini-*)
                             family_of(candidate) → dispatch to right backend
```

No separate `gemini_routing_overrides` table needed. The existing
`model_pin_overrides` + `_model_for()` + family-dispatch routing handles
everything once `call_llm` is fixed in §1a.

---

## 7. Implementation order (recommended PR sequence)

### PR A — routing fix (the load-bearing change)

`src/llm/cli.py`: rewrite `call_llm` backend resolution (§1a).
`src/llm/gemini_backend.py`: remove `subscription=True` from `GEMINI_BACKEND_ALLOWED_PURPOSES`
  docstring; deprecate `gemini_allowed_purposes()` (leave callable, return empty,
  log deprecation warning so no callers break).
`src/llm/model_ladder.py`: update Gemini prices to real API values (§2).
`src/llm/gemini_backend.py`: remove $0 pin in `usage_meta_from_gemini_stats` (§1c).

Tests: `test_call_llm_routes_by_model_family` (new), existing tests should still
pass (the explicit `backend=` override path is unchanged).

### PR B — token efficiency in eval

`src/llm/model_eval.py`: add `output_tokens`/`input_tokens` to `ModelRunResult`;
  add `token_efficiency_ratio` + `*_tokens_mean` to `CandidateVerdict`.
`execution/run_model_eval_sweep.py`: pass token data through to `_evaluate_candidate`
  and include in `case_audit` / `summary_json`.
`src/llm/model_overrides.py`: add `token_efficiency_ratio` column to the
  `record_verdict` call (or store in `summary_json` — no migration needed for
  the latter, simpler).

Tests: `test_candidate_verdict_includes_token_efficiency` (new).

### PR C — wire `news_structuring` golden set + add eval AuditSpecs for candidates

`src/evals/coverage.py`: add `news_structuring` to `GOLDEN_PURPOSES`.
`evals/rubrics/earnings_themes_split.md` + `qa_topics.md`: new rubric specs.
`src/evals/rubric_judge.py AUDIT_SPECS`: register them.

### PR D — first promotion (after PRs A-C, owner sign-off)

After the golden-set cert run shows ≥95% pass rate for the five Haiku purposes
and `peer_selection`/`podcast_takeaway_summary` on the Gemini backend:
`src/llm/cli.py LLM_MODELS`: update those purposes to `gemini-2.5-flash` or
  `gemini-3.1-pro-preview`. That's the only change needed — no allowlist, no
  separate table.

**Owner sign-off required** before PR D merges. Present the cert table from the
golden-set run and the sibling chip's `peer_selection` verdict.

---

## 8. Guardrails

- **Fail-closed is already wired.** Operational Gemini failures in `call_llm`
  degrade to Claude (the try/except block). This is unchanged.
- **Explicit `backend=` still works.** The compare harness (`compare_backends.py`)
  can still force a specific backend. `run_model()` in `model_eval.py` already
  uses `family_of(model_id)` to dispatch — it will auto-use the right backend
  once `call_llm` is fixed.
- **JSON-strictness.** Gemini Pro sometimes wraps outputs in markdown fences.
  The golden-set cert (PR C) will catch this — a pass rate < 95% on
  `format` means the prompt needs a fence-strip before the model goes to prod.
- **Rate limits.** Consumer OAuth tier: ~60 req/min, ~1000 req/day. With real
  API prices as the cost basis, the decision to use Gemini is cost-driven not
  "free" — monitor `llm_calls` for 429s and flag high-volume purposes
  (`qa_topics`: ~21 calls/day) before promotion.
- **Token efficiency flag.** Surface `token_efficiency_ratio > 1.5` in the
  SWITCH_DOWN reason even when quality holds, so the dashboard shows both
  savings dimensions (price/token AND tokens/call).

---

## 9. Sibling chip coordination

The `peer_selection` model eval chip is evaluating Claude Sonnet/Opus vs Gemini
Pro/Flash concurrently. Cite its result in PR D:
- `PROMOTE_CANDIDATE` → set `"peer_selection": "gemini-3.1-pro-preview"` in `LLM_MODELS`
- `KEEP_INCUMBENT` or `HOLD` → leave at Sonnet; the weekly sweep will accumulate more evidence

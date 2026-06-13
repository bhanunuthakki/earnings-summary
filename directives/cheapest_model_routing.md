# Directive: cheapest-at-parity model routing (incl. Gemini backend)

**Goal:** route every LLM purpose to the cheapest model that holds parity with the
incumbent.  Gemini consumer-subscription ($0 marginal) is already wired and
evaluated in the weekly sweep — but zero purposes route there in production. This
directive explains exactly why, quantifies the opportunity, and lays out the
eval-gated path to fix it.

---

## 1. Production routing resolution order (precise)

```
call_llm(prompt, purpose=P, model=None, backend=None)
  │
  ├─ backend check (gemini_allowed_purposes)
  │     = GEMINI_BACKEND_ALLOWED_PURPOSES (frozenset — ships EMPTY)
  │       ∪ GEMINI_BACKEND_PURPOSES env var (unset in prod crons)
  │     → always empty → resolved_backend = "claude"
  │
  └─ Claude path → _model_for(P)
        1. active_override(P)   DB: model_pin_overrides WHERE active=1
        2. LLM_MODELS[P]        hardcoded code pin
        3. DEFAULT_MODEL        fallback (warns if P unknown)
        → _call_claude(prompt, model=resolved_model)
              → claude -p --model <id> --output-format json
              → on operational failure → gemini API-key fallback (metered, NOT the backend)
```

**Billing path:** `ANTHROPIC_API_KEY` is set in this project's `.env` (per
`feedback_anthropic_api_key.md`), so `claude -p` bills against the metered
Anthropic API, not the subscription.  Every call lands in `llm_calls` via
`record_llm_call` / `parse_claude_json_output`; `cost_estimate_usd` comes from
the Claude JSON envelope's usage metadata.

---

## 2. Why Gemini is unused in production — three stacked reasons

### 2a. Allowlist ships empty (intended gate)
`GEMINI_BACKEND_ALLOWED_PURPOSES = frozenset()` in `src/llm/gemini_backend.py`.
No code or env var in production cron definitions adds to it.  This is the
correct safety mechanism: a purpose may enter this set only after the pairwise
judge certifies its Gemini output quality.

### 2b. No auto-promotion path to the Gemini allowlist
Both promotion paths are advisory-only:
- `execution/compare_backends.py` + `execution/grade_backends.py` produce a
  per-purpose `PROMOTE_CANDIDATE` recommendation — but the operator still edits
  `GEMINI_BACKEND_ALLOWED_PURPOSES` manually.
- `execution/run_model_eval_sweep.py` evaluates Gemini candidates (via
  `cheaper_candidates(incumbent)` → Gemini models appear because
  `model_ladder.py` ranks them as cheapest) and writes `model_eval_verdicts`.
  `apply_model_switches` reads those verdicts and writes `model_pin_overrides`.
  But `model_pin_overrides` stores only a model ID string — `call_llm` reads it
  via `_model_for()` on the CLAUDE path and then calls
  `_call_claude(model="gemini-3.1-pro-preview")`, which passes that ID to
  `claude -p --model gemini-3.1-pro-preview` → Claude CLI rejects an unknown
  model ID → operational failure → falls through to the metered API-key Gemini
  fallback.  **The existing auto-switch loop is broken for cross-backend
  switches**: it evaluates Gemini correctly in the sweep but the pin mechanism
  cannot route to the Gemini backend.

### 2c. Weekly sweep only harvests two purposes
`_HARVEST_STEPS` in `run_weekly_model_eval.py` only captures `bear_case` and
`company_description`.  Only 2 Gemini verdicts exist in `model_eval_verdicts`
(both `qa_topics`, `INSUFFICIENT_DATA` — 1 case each from a manual run, not
the weekly job).  Zero `model_pin_overrides` rows.

---

## 3. Cost table (last 19 days: 2026-05-25 → 2026-06-13)

| Purpose | Model | Calls | Cost (19d) | Ann. est. | Gemini viable? | Eval exists? |
|---|---|---|---|---|---|---|
| `bear_case` | Sonnet | 166 | $97.84 | ~$1,880 | **NO** (Gemini lost 4/4, REJECT) | outcome grader |
| `recent_developments` | Sonnet+web | 194 | $69.80 | ~$1,340 | **NO** (needs web tools) | none |
| `earnings_themes_split` | Sonnet | 156 | $55.02 | ~$1,057 | candidate | none |
| `qa_topics` | Sonnet | 400 | $22.65 | ~$435 | candidate | none |
| `exec_comp_alignment` | Opus | 145 | $18.97 | ~$365 | unknown (not in LLM_MODELS) | none |
| `saydo_filter` | Sonnet | 259 | $18.51 | ~$356 | candidate | none |
| `pairwise_analysis` | Sonnet | 39 | $13.13 | ~$252 | candidate | none |
| `company_description` | Opus/Sonnet | 50 | $13.20 | ~$254 | candidate | none |
| `valuation_basis` | Opus | 122 | $11.76 | ~$226 | **RISKY** (sector judgment) | none |
| `news_structuring` | Opus | 20 | $11.60 | ~$223 | candidate | golden file |
| `backend_compare_judge` | Opus | 38 | $6.40 | ~$123 | NO (meta/judge) | meta |
| `canonicalize_segments` | Haiku | 70 | $4.19 | ~$80 | candidate | none |
| `kpi_registry_auto_proposal` | Opus | 22 | $4.16 | ~$80 | **RISKY** (drives alert thresholds) | none |
| Fast-classifier cluster | Haiku | ~130 | ~$2.2 | ~$42 | **SAFE** (golden sets) | golden |

**Total (19d): $400.19 · Ann. est.: ~$7,700.** Period was high-activity (S11/S12 PRs
in flight), so steady-state may be lower; treat ann. as an upper bound.

**Conservative addressable opportunity** (purposes with viable Gemini path, excl.
bear_case/recent_developments/valuation_basis/exec_comp_alignment):
`earnings_themes_split` + `qa_topics` + `saydo_filter` + `pairwise_analysis` +
`company_description` + `news_structuring` = $133.11 / 19d → **~$2,560/year**
if those purposes move to Gemini at parity.  The fast-classifier cluster adds
~$42/year but is already very cheap on Haiku.

---

## 4. Safe vs risky candidate split

### SAFE — deterministic eval or structured golden set; certify cheaply

| Purpose | Incumbent | Gemini target | Eval mechanism |
|---|---|---|---|
| `viewspec_compile` | Haiku | gemini-3.5-flash | golden set (objective: valid ViewSpec JSON) |
| `transcript_metadata` | Haiku | gemini-3.5-flash | golden set (ticker_Q_YYYY format) |
| `intake_classifier` | Haiku | gemini-3.5-flash | golden set (doc-type enum) |
| `decision_conditions_extract` | Haiku | gemini-3.5-flash | golden set (JSON schema) |
| `ask_pack_router` | Haiku | gemini-3.5-flash | golden set (closed enum pick) |
| `peer_selection` | Sonnet | gemini-3.1-pro-preview | golden set; sibling chip running — cite its result |
| `podcast_takeaway_summary` | Sonnet | gemini-3.1-pro-preview | golden set (podcast_takeaway_summary.json) |

These five Haiku-class purposes + Sonnet `peer_selection` and `podcast_takeaway_summary` can be
certified by running the deterministic golden-set grader through the Gemini backend and checking
the pass rate.  No pairwise judge needed for the structured-output ones — Gemini either produces
valid JSON that passes the schema or it doesn't.  **Run
`python execution/compare_backends.py --smoke` first to confirm the backend is
alive, then run the golden-set grader with `backend="gemini"` forced for each.**

### CANDIDATE — high cost, needs eval harness before promotion

| Purpose | Incumbent | Ann. est. | Blocker |
|---|---|---|---|
| `earnings_themes_split` | Sonnet | ~$1,057 | No eval; build rubric or golden set first |
| `qa_topics` | Sonnet | ~$435 | No eval; Q&A topic quality is assessable via rubric |
| `saydo_filter` | Sonnet | ~$356 | No eval; filter accuracy can be golden-set tested |
| `pairwise_analysis` | Sonnet | ~$252 | No eval; pairwise quality needs rubric |
| `company_description` | Sonnet | ~$254 | No eval; rubric is the right gate |
| `news_structuring` | Opus | ~$223 | golden file exists but not in GOLDEN_PURPOSES; wire it |
| `canonicalize_segments` | Haiku | ~$80 | No eval; structural output — golden set feasible |

For these, the path is: add an eval mode (golden set or rubric AuditSpec) →
run `compare_backends.py --from-capture` → grade with `backend_judge` dual
judge → PROMOTE_CANDIDATE → Phase 2 auto-promotion.

### RISKY / NEVER — keep on Claude unless very strongly certified

| Purpose | Reason |
|---|---|
| `bear_case` | Gemini REJECTED 4/4 (adversarial analytical reasoning; keep incumbent) |
| `recent_developments` | Needs web tools; Gemini backend has none — structurally unfair comparison |
| `valuation_basis` | Sector/business-model judgment (Opus); one call per ticker; wrong sector multiple is silent harm |
| `material_news_classification` | Materiality veto — alerts fire based on this; false-negative = missed alert |
| `kpi_registry_auto_proposal` | Drives alert threshold calibration; Opus for instruction-following |
| `earnings_tone_diff` | High-stakes alert trigger; needs Opus instruction-following |
| `saydo_importance` | Ranking discipline matters; Opus |
| `advisor_*` | Portfolio advice; Opus judgment tier; never route to unverified backend |
| `backend_compare_judge` | Meta; the judge must outperform both contestants |

---

## 5. The architectural fix (prerequisite for Phase 2)

The current `model_pin_overrides` stores only a model ID string.  When
`_model_for(purpose)` returns a Gemini model ID on the Claude path, `call_llm`
passes it to `_call_claude()` (Claude CLI with a Gemini model ID), which fails.
The auto-switch loop therefore **cannot safely promote a purpose to Gemini via
`model_pin_overrides`** — the existing mechanism is Claude-tier-only by
construction.

**Required fix (minimal):** add a DB-backed companion to `gemini_allowed_purposes()`.

```sql
-- new table (one migration)
CREATE TABLE gemini_routing_overrides (
    purpose     TEXT PRIMARY KEY,
    model       TEXT NOT NULL,          -- gemini-* model id
    set_by      TEXT NOT NULL,
    set_at      DATETIME NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    reason_json TEXT
);
```

`gemini_allowed_purposes()` becomes:
```python
def gemini_allowed_purposes() -> frozenset[str]:
    code_set = GEMINI_BACKEND_ALLOWED_PURPOSES
    env_set  = {p.strip() for p in os.environ.get(GEMINI_BACKEND_PURPOSES_ENV_VAR, "").split(",") if p.strip()}
    db_set   = _load_db_routing_overrides()   # reads gemini_routing_overrides WHERE active=1
    return code_set | env_set | db_set
```

`_model_for()` is unchanged (it's only on the Claude path); `gemini_model_for(purpose)` reads
the DB row's model column for DB-routed purposes.

`apply_model_switches` is extended: when the candidate is a Gemini model ID
(`family_of(candidate) == GEMINI`), write to `gemini_routing_overrides` instead
of `model_pin_overrides`.  Auto-demote on regression: KEEP_INCUMBENT streak →
deactivate the `gemini_routing_overrides` row.

**Until this fix ships:** the weekly sweep should skip Gemini candidates in
`apply_model_switches` (already safe because no SWITCH_DOWN verdicts exist for
Gemini candidates — only 2 INSUFFICIENT_DATA rows — but guard explicitly):
```python
# In apply_model_switches.evaluate_switches():
from llm.model_ladder import family_of, GEMINI
if family_of(candidate) == GEMINI:
    # Gemini routing requires gemini_routing_overrides, not model_pin_overrides.
    # Skip until that table + routing extension lands (directives/cheapest_model_routing.md).
    results.append(SwitchResult(purpose, candidate, "GEMINI_SKIP", None,
        "Gemini routing requires gemini_routing_overrides; skip until Phase 2 ships"))
    continue
```

---

## 6. Eval-gated path to cheapest-at-parity routing

### Phase 1 (advisory, cheap — no code flag flip, owner-approved promotions)

**1a. Run the deterministic safe-candidate cert:**
```bash
# 1. Confirm the backend is alive
python execution/compare_backends.py --smoke --repo-root <MAIN>

# 2. Run the golden-set graders through Gemini (set env var for local trial)
GEMINI_BACKEND_PURPOSES=viewspec_compile,transcript_metadata,intake_classifier,\
decision_conditions_extract,ask_pack_router \
python execution/run_evals.py --coverage --repo-root <MAIN>
```
If pass rate ≥ 95% on the golden set under Gemini, the purpose is safe to
promote.  Log the pass rate in the PR that adds the purpose to
`GEMINI_BACKEND_ALLOWED_PURPOSES`.

**1b. Cite the peer_selection sibling chip result.**  If the sibling chip's eval
is PROMOTE_CANDIDATE for `peer_selection` on Gemini, include it as the first
Sonnet-class promotion in the same PR.

**1c. Harvest + compare for the high-cost candidates:**
```bash
# Harvest bear_case (already in weekly sweep — already REJECTED; don't re-run)
# Harvest earnings_themes_split and qa_topics from a real build
LLM_CAPTURE_DIR=data/llm_capture \
python execution/build_artifacts.py --enable-llm --ticker NU --repo-root <MAIN>

# Replay each through Gemini
python execution/compare_backends.py --from-capture data/llm_capture/capture_<date>.jsonl \
    --purpose earnings_themes_split --repo-root <MAIN>
python execution/compare_backends.py --from-capture data/llm_capture/capture_<date>.jsonl \
    --purpose qa_topics --repo-root <MAIN>

# Dual-judge grade
python execution/grade_backends.py --repo-root <MAIN>
```
Output: `data/backend_compare/summary_<runid>.json`.  Cite the summary in the
PR when recommendation == PROMOTE_CANDIDATE for both judges.

**1d. Add eval modes for high-cost candidates lacking them:**
Priority order by addressable cost:
- `earnings_themes_split`: rubric AuditSpec (compare quarterly theme relevance,
  prepared vs Q&A split fidelity, citation discipline against transcript).
- `qa_topics`: rubric AuditSpec (topic coverage vs transcript, no hallucinated
  questions, format compliance).
- `news_structuring`: wire the existing `evals/golden/news_structuring.json`
  into `GOLDEN_PURPOSES` (the file exists but is not exercised).
- `saydo_filter` + `pairwise_analysis`: golden set feasible (both have
  structured output with assessable correctness).

### Phase 2 (auto-promotion — gated on Phase 1 landing)

Fold the Gemini backend into the weekly sweep's auto-switch path:

1. **Ship `gemini_routing_overrides` table** (migration) and extend
   `gemini_allowed_purposes()` to read it.
2. **Extend `apply_model_switches`** to write `gemini_routing_overrides` for
   Gemini candidates (instead of `model_pin_overrides`), with the same
   consecutive-streak guardrail (3× SWITCH_DOWN to promote, 3× KEEP_INCUMBENT
   to demote).
3. **Extend `_HARVEST_STEPS`** in `run_weekly_model_eval.py` to cover the top-cost
   candidate purposes (`earnings_themes_split`, `qa_topics`) so captures
   accumulate weekly.
4. **Auto-demote:** when `_is_consecutive(keep_recent, KEEP_INCUMBENT, consecutive_keep)`
   for a Gemini-overridden purpose, deactivate the `gemini_routing_overrides` row
   and fire a regression alert — identical behavior to the Claude-tier revert.

---

## 7. Guardrails to preserve

- **Parity eval IS the quality gate.** Never route to a cheaper model because
  it's cheap; route because the judges certified it holds parity on real prompts
  for that specific purpose.  The cost of a silent quality regression outweighs
  any $ saving.
- **Gemini consumer-tier limits.** ~60 req/min, ~1000/day (Login with Google
  free plan). High-volume purposes (`qa_topics` — 400 calls / 19d, ~21/day) are
  near the per-day limit.  Surface `rate_limited=True` in the promotion log and
  monitor for 429s after enabling.
- **Pro→Flash transparent reroute.** Under load the Gemini consumer tier can
  silently reroute Pro calls to Flash.  Token stats in the ledger carry the
  actual model.  Monitor the `model` column in `llm_calls` for unexpected Flash
  rows on Pro-pinned purposes.
- **JSON-strictness differences.** Gemini Pro sometimes wraps outputs in markdown
  fences even when instructed not to.  Eval the structured-output purposes
  (viewspec, classifiers) with strict schema validation — a pass rate < 95% on
  format compliance means the prompt needs a fence-strip or instruction tweak
  before promotion.
- **Fail-closed already wired.** An allowlist-routed Gemini call that fails
  operationally degrades to Claude (in `call_llm`).  This means enabling a
  purpose can NEVER break the pipeline — the worst case is Claude does the work.
- **High-stakes purposes.** `valuation_basis`, `material_news_classification`,
  `kpi_registry_auto_proposal`, `earnings_tone_diff`, `advisor_*` stay Claude
  unless very strongly certified (dual-judge PROMOTE_CANDIDATE with n ≥ 10,
  cross-judge agreement ≥ 0.80, margin > 0.5) AND owner explicitly signs off.

---

## 8. Recommended first PR

**Scope:** advisory only — no production routing changes.

1. Add guard to `apply_model_switches` to skip Gemini candidates (prevents a
   future accidental broken pin if the weekly sweep ever accumulates 3×
   SWITCH_DOWN for a Gemini model).
2. Wire `evals/golden/news_structuring.json` into `GOLDEN_PURPOSES` (it exists
   but is unreachable through the coverage/eval harness).
3. Run deterministic golden-set cert for the five Haiku-class safe purposes
   (local `GEMINI_BACKEND_PURPOSES` env trial); record results in
   `data/backend_compare/golden_cert_<date>.json`.
4. Commit this directive + the cert results.
5. **Owner decision:** present the cert table.  If pass rates ≥ 95%, owner adds
   the certified purposes to `GEMINI_BACKEND_ALLOWED_PURPOSES` in the same PR.
   That is the only production routing change in Phase 1 — and it's fail-closed
   (operational Gemini failures degrade to Claude).

**Owner sign-off required before any production routing change.** Present the
cert table from step 3 and the sibling chip's `peer_selection` verdict;
do not flip `GEMINI_BACKEND_ALLOWED_PURPOSES` or add to `gemini_routing_overrides`
without explicit approval.

---

## 9. Sibling chip coordination

The `peer_selection` model eval chip is evaluating Claude Sonnet/Opus vs Gemini
Pro/Flash for that purpose concurrently.  Do NOT re-run that comparison here;
cite its result in the Phase 1 PR:

- If sibling verdict = `PROMOTE_CANDIDATE` for Gemini Pro on `peer_selection` →
  include `peer_selection` in the first-promotion list.
- If `KEEP_INCUMBENT` or `HOLD` → leave it out; the weekly sweep will
  accumulate more evidence before the next review.

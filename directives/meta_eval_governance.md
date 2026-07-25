# Directive: meta-eval governance — the optimizer that steers itself

**Status: BUILT 2026-07-02 — owner reviewed §9 same day; decisions LOCKED in §10
(which overrides any earlier recommendation it conflicts with); all six build
phases merged same day (#749 #750 #751 #752 #753 #754). EXTENDED 2026-07-24/25
(owner-authorized): §4.7 — the §4 prompt loop was a dead circuit (zero
experiments ever) and is now a randomized, self-steering cycle (#1005 #1010
#1014), hardened against the July-2026 transport outage (#1008). Residual open
items: 3-family RISKY judging (§10 Q5a, deferred past PR6), the Q6 purpose=None
deprecation PR (after the SayDo trailing window clears), wiring nominator
`prompt_experiment` rows into the §4.7 cycle, and §4.7.6's list.**

Extends — does NOT replace — the existing spine:
`directives/model_eval_loop.md` (the downgrade loop), `directives/cheapest_model_routing.md`
(model-first routing + the cost basis), `directives/gemini_backend.md` (backend mechanics),
`directives/llm_evals_plan.md` (the general eval harness). The pairwise judge
(`src/llm/backend_judge.py`), the verdict engine (`src/llm/model_eval.py`), the sweep
(`execution/run_model_eval_sweep.py` + `execution/run_weekly_model_eval.py`), the auto-switch
(`execution/apply_model_switches.py` + `src/llm/model_overrides.py`), capture
(`src/llm/capture.py`) and the ledger (`llm_calls`) are all REUSED as-is unless a diff is
explicitly specified below.

---

## 0. Problem statement

The pareto loop ("keep the incumbent unless a cheaper model is PROVABLY at parity") works
mechanically, but every **steering input** is still hand-maintained or naive:

1. **Which purposes to test** — `LLM_MODELS` in `src/llm/cli.py` plus the hand-written
   safe/candidate/risky tables in `cheapest_model_routing.md` §5. The sweep's
   `_discover_active_purposes` treats a $112/mo purpose and a $0.30/mo purpose identically.
2. **Which prompts to grade** — `_load_cases_from_files` takes the newest ≤6 captured prompts
   per purpose: a convenience sample biased to the last harvest's tickers and the modal easy
   case. `min_n=4`. With that variance no honest verdict clears the conservative switch bar —
   which is one reason `model_pin_overrides` has **zero rows ever** (2026-07-01 audit).
3. **What "good" means per case** — four fixed facets (faithfulness/accuracy/format/
   conciseness). For a `bear_case` prompt about NU, "faithfulness" won't reliably catch a
   candidate that skips the NPL scope-switch the prompt explicitly demanded.
4. **Prompt quality itself** — `prompt_versions.py` can A/B-compare versions, but a human must
   invent v2. Nothing generates or tests improved prompts.
5. **Bias hygiene** is currently guaranteed by convention (capture-off during sweeps,
   brand-blind judge), not by a named, tested contract — and every new subsystem above is a
   new opportunity to leak eval framing into the thing being measured.

Ledger ground truth (main repo `data/portfolio.db`, 30d to 2026-07-02): **$706 / 4,975 calls**.
Top addressable lines: `bear_case` $112 (Sonnet), `news_structuring` $81 (Opus, 2-case golden
set), `recent_developments` $78 (web — downgrade-ineligible, A/B-eligible),
`earnings_themes_split` $60, `exec_comp_alignment` $28 (Opus). The machinery designed here
costs ~$10–20/mo (§8) against >$150/mo of measurable headroom.

A third backend (**OpenRouter**: DeepSeek / Qwen / Llama / …) is being built in parallel.
Everything below treats "candidate model" as *any entry in `model_ladder.MODEL_LADDER`
cheaper than the incumbent* — never as "Claude or Gemini" (§6).

### Scope taxonomy (used everywhere below)

One new shared constant, e.g. in `src/llm/eval_scopes.py` (or folded into
`workload_inventory.py`):

```python
# Ledger scopes that mark measurement traffic, not production workload.
# model_eval / backend_judge already exist; prompt_ab + meta_eval are new (§4, §1-§3).
EVAL_SCOPES: frozenset[str] = frozenset({"model_eval", "backend_judge", "prompt_ab", "meta_eval"})
```

Every meta-machinery LLM call carries one of these scopes; every workload/cost rollup
excludes them. This is the single source of truth for "don't let the optimizer observe
itself" (§5, invariant I5).

---

## 1. Subsystem 1 — dynamic workload inventory + the Opus nominator

Replaces: the static mental model of `LLM_MODELS` as "the list", the hand-maintained cost
table in `cheapest_model_routing.md` §4, and the hand-maintained safe/risky split in §5.

### 1.1 Deterministic inventory (no LLM — this is the floor)

New module `src/llm/workload_inventory.py`:

```python
@dataclass(frozen=True, slots=True)
class PurposeWorkload:
    purpose: str
    incumbent_model: str            # resolved like _model_for: model_pin_overrides -> LLM_MODELS -> DEFAULT_MODEL
    calls_30d: int
    cost_usd_30d: float
    distinct_prompts_30d: int       # COUNT(DISTINCT prompt_sha256) — repeat-shape signal
    avg_prompt_chars: float
    web_scoped: bool                # any scope='web' row: downgrade-INELIGIBLE (structural), A/B-eligible
    cheapest_candidate: str | None  # model_ladder.cheaper_candidates(incumbent)[0] if any
    headroom_usd_30d: float         # the leverage score, see below
    last_verdicts: tuple[str, ...]  # newest-first per best candidate from model_eval_verdicts,
                                    # CANDIDATE_ERRORED rows excluded (infra, not quality — #723)
    eval_modes: tuple[str, ...]     # from evals.coverage (golden/audit/outcome/meta)
    budget_capped: bool             # llm_budgets row with on_exceed != 'warn'
```

Core query (mirrors the audit SQL; naive-UTC cutoff per repo convention):

```sql
SELECT purpose, COUNT(*) AS calls, SUM(COALESCE(cost_estimate_usd, 0)) AS usd,
       COUNT(DISTINCT prompt_sha256) AS uniq, AVG(prompt_chars) AS avg_chars,
       MAX(CASE WHEN scope = 'web' THEN 1 ELSE 0 END) AS web
FROM llm_calls
WHERE called_at >= :cutoff_30d
  AND purpose IS NOT NULL
  AND (scope IS NULL OR scope NOT IN ('model_eval','backend_judge','prompt_ab','meta_eval'))
GROUP BY purpose
```

**Leverage score** (the rank key; deterministic, auditable):

```
headroom_usd_30d = cost_usd_30d * (1 - blended(cheapest_candidate) / blended(incumbent))
```

with `blended` = `model_ladder.ModelCost.blended_usd_per_mtok`. A purpose already at the
ladder floor scores 0. Volume and prompt size are already priced into `cost_usd_30d`, so this
one number IS "cost × volume × current-tier headroom". Web-scoped purposes get
`headroom_usd_30d = 0` for *model* nominations (candidates have no web tools —
`gemini_backend.md` precedent) but stay rankable for *prompt* experiments (§4).

The inventory also emits the `purpose IS NULL` + unregistered-purpose cost lines (reusing
`model_eval_panel.load_anon_costs` semantics) so the nominator sees the whole surface — but
NULL rows are not nominable; they route to the existing Optimizer-panel alarm.

CLI: `python execution/report_workload_inventory.py --repo-root <MAIN>` renders the table
(replaces re-deriving §4 of `cheapest_model_routing.md` by hand). Pure read; safe anywhere.

### 1.2 The Opus nominator (`optimizer_nominator`)

A scheduled LLM call that reads the inventory rollup and returns **ranked nominations with
rationale** — the judgment layer the deterministic score can't provide: risk tiering ("this
purpose vetoes alerts; a false-negative downgrade is silent harm"), family grouping ("these
three share one prompt scaffold — certify together"), new-model awareness ("the ladder gained
deepseek-v4; re-test the whole Haiku tier"), and prompt-experiment nominations (§4).

**Input** (one compact JSON document, ~8–15K chars):
- per-purpose `PurposeWorkload` rows (top ~40 by cost; the tail is noise),
- the full `MODEL_LADDER` (id, family, prices, flags) — the closed vocabulary of candidates,
- last-3 verdict summaries per (purpose, candidate) from `model_eval_verdicts`,
- the static risk notes: move `cheapest_model_routing.md` §5's RISKY table into a
  reviewable constant `RISK_NOTES: dict[str, str]` in `workload_inventory.py` (e.g.
  `"material_news_classification": "alert veto; false-negative = missed alert"`). The
  nominator receives them as *hints*, and its output risk tier may only be **stricter**,
  never looser, than the static note (enforced in validation).

**Output schema** (via `llm.structured.call_llm_structured`, `expect="object"`;
`StructuredParseError` → deterministic fallback, hard-stops propagate per `is_hard_stop`):

```json
{"nominations": [
  {"purpose": "news_structuring",
   "priority": 1,
   "kind": "model_downgrade",              // or "prompt_experiment"
   "candidates": ["claude-sonnet-4-6", "gemini-3.1-pro-preview"],
   "why": "Opus at $81/30d on a copy-the-table task; 2-case golden set is the blocker, not quality risk",
   "risk_tier": "candidate",               // safe | candidate | risky
   "suggested_min_n": 10},
  ...
]}
```

**Validation (fail-closed, closed-vocabulary — the `key_metrics` pattern):**
- every `purpose` must exist in the supplied inventory; unknown → row dropped + logged;
- every candidate must be in the supplied ladder AND in
  `cheaper_candidates(incumbent, include_openrouter=True)` — **the nominator can never
  nominate a lateral or more expensive model; the deterministic ladder stays the guard**.
  (`include_openrouter=True` here is deliberate: a validated nomination IS the opt-in act
  the ladder's OpenRouter comment reserves for the meta-eval — see §6);
- `risk_tier` may only tighten `RISK_NOTES`;
- ≤ `MAX_NOMINATIONS` (default 8) rows accepted, by priority.

**Fallback:** if the call fails or validates to zero rows, nominations = deterministic top-K
by `headroom_usd_30d` with `source='deterministic_fallback'`. The loop must never stall on
its own steering call.

### 1.3 Data model

New table `optimizer_nominations` (one Alembic migration; pick the number + down_revision at
rebase time — head is `0131_coach_pings` as of 2026-07-02; see the parallel-session collision
memory):

```sql
CREATE TABLE optimizer_nominations (
  id INTEGER PRIMARY KEY,
  nomination_run_id TEXT NOT NULL,       -- uuid4 hex per nominator invocation
  purpose TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('model_downgrade','prompt_experiment')),
  priority INTEGER NOT NULL,
  headroom_usd_30d REAL, cost_usd_30d REAL, calls_30d INTEGER,
  incumbent_model TEXT NOT NULL,
  candidates_json TEXT NOT NULL,          -- JSON array of ladder model ids
  rationale TEXT NOT NULL,
  risk_tier TEXT NOT NULL CHECK (risk_tier IN ('safe','candidate','risky')),
  suggested_min_n INTEGER,
  source TEXT NOT NULL CHECK (source IN ('opus','deterministic_fallback')),
  ladder_sha TEXT NOT NULL,               -- sha256 of the serialized MODEL_LADDER at nomination time
  status TEXT NOT NULL DEFAULT 'pending'  -- pending | swept | skipped | expired
    CHECK (status IN ('pending','swept','skipped','expired')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL   -- naive-UTC ISO
);
CREATE INDEX ix_optimizer_nominations_status ON optimizer_nominations (status, priority);
```

### 1.4 Feeding the sweep

- `run_model_eval_sweep.run_sweep` gains `nominations: list[Nomination] | None = None`
  (and the CLI a `--from-nominations` flag). When present: the purpose list = pending
  nominations ordered by priority; per-purpose candidate list =
  `nomination.candidates ∩ cheaper_candidates(incumbent, include_openrouter=True)`
  (re-checked at sweep time — the incumbent may have moved; the OpenRouter axis is
  reachable ONLY through a nomination, per §6); per-purpose `min_n` =
  `max(args.min_n, suggested_min_n)`. When absent: today's behavior (all active purposes,
  `include_openrouter=False` default), unchanged.
- `run_weekly_model_eval.py` gains **step 0** before harvest: refresh the inventory; if the
  newest nomination run is older than the nominator cadence OR its `ladder_sha` differs from
  the current ladder, invoke the nominator; mark superseded runs `expired`. Harvest then
  prioritizes tickers/purposes the pending nominations need (extends `_HARVEST_STEPS`
  selection rather than replacing rotation).
- `apply_model_switches` is untouched as the sole switch authority. Nominations steer
  *measurement*, never routing.

### 1.5 New LLM purposes introduced

`optimizer_nominator` — **operational recipe** (per the operational-purpose memory: touch
`LLM_MODELS` + `prompt_versions` ONLY; this is NOT an eval purpose, so the 4-registries
lockstep does not apply). Plus three hygiene touches every meta purpose in this directive
gets:
- `src/llm/cli.py LLM_MODELS["optimizer_nominator"] = "claude-opus-4-8"` (judgment tier;
  ~1–2 calls/month; rationale comment per `llm_calls.md` rule 3),
- `src/llm/prompt_versions.py` entry `"v1"`,
- `src/evals/coverage.py META_PURPOSES` += the purpose (it grades/steers others; keeps the
  coverage report from flagging it uncovered),
- `src/llm/capture.py CAPTURE_DENYLIST` += the purpose (its traffic must never enter a
  harvest corpus),
- `llm_budgets` seed row in the same migration ($3/mo, `on_exceed='warn'` — 0083/0089
  precedent).

### 1.6 Cadence

- Deterministic inventory: computed at every weekly sweep (cheap SQL) + on-demand CLI.
- Opus nominator: **monthly** (first Sunday, inside `run_weekly_model_eval` step 0) + forced
  when `ladder_sha` changes (a new model landing in the ladder is exactly when re-nomination
  pays) + on-demand via `--nominate`. Weekly Opus would re-rank a mostly static list — the
  cheapest-model-per-job taste says don't.

### 1.7 Failure modes

- Nominator hallucinates purposes/models → closed-vocabulary validation drops rows (logged
  `optimizer_nomination_rejected`), never executed.
- Nominator down / unparseable → deterministic fallback; sweep proceeds.
- Stale nominations after routing changed → `candidates ∩ cheaper_candidates(incumbent)`
  re-check at sweep time; empty intersection ⇒ status `skipped`.
- Self-nomination of eval machinery → impossible: meta purposes ride `EVAL_SCOPES` scopes and
  are excluded by the inventory SQL; belt-and-braces, `META_PURPOSES` are filtered from the
  nominable universe too.
- Cost double-count → same scope exclusion (eval traffic never inflates a purpose's leverage).

---

## 2. Subsystem 2 — statistical randomized sampling (stratified, LLM-assisted)

Replaces: `_load_cases_from_files`'s newest-6 convenience sample.

### 2.1 Census, frame, and the honesty metric

- **Census** (population): per purpose, all distinct `prompt_sha256` in `llm_calls` over 90d
  (production scopes only), with `prompt_chars`, `ticker`, `scope`, and per-sha call count
  (recurrence weight). The ledger is sha-only by design — it can't provide text, but it
  defines what "representative" means.
- **Frame** (what we can actually replay): captured prompts from `LLM_CAPTURE_DIR` JSONL
  (which have full text + the incumbent response), keyed by the same `prompt_sha256`.
- **`frame_share` = |frame ∩ census| / |census|** per purpose, recorded in the sweep audit.
  Below `MIN_FRAME_SHARE` (default 0.30) or below the stratum quota, the purpose's verdict
  this sweep is **`INSUFFICIENT_FRAME`** (a new advisory label recorded in
  `model_eval_verdicts.verdict`, treated like `INSUFFICIENT_DATA` by `apply_model_switches`,
  i.e. streak-neutral) and the weekly orchestrator queues extra harvest for it next run.
  Never grade a bad sample and call it evidence.

### 2.2 Strata

Deterministic axes first (free):
1. **prompt-length tercile** — census-percentile boundaries per purpose (short/medium/long);
2. **ticker cap** — max `ceil(n/3)` cases from any one ticker (kills the "whatever the last
   harvest built" bias);
3. **scope** — `web` vs plain kept separate (web replays are structurally confounded).

LLM-assisted axis (the clever part):

**New purpose `case_difficulty_classify`** (FAST tier — `FAST_CLASSIFIER_MODEL`): given the
captured prompt (truncated ~6K chars) + the purpose name, emit

```json
{"difficulty": "easy" | "moderate" | "hard",
 "case_type": "<free label, <=6 words>",
 "hard_signals": ["conflicting units", "sparse KPI catalog", ...]}
```

Rules that make this sound:
- the classifier reads **the prompt only** — never the incumbent response, never any
  candidate output — so stratification cannot encode outcome knowledge;
- classification is a pure function of the prompt ⇒ **cached forever** in
  `eval_case_features` keyed `(purpose, prompt_sha256, classifier_version)`; one Haiku call
  per new distinct prompt, ever;
- prompt text is treated as data (the untrusted-spotlighting convention,
  `src/llm/untrusted.py`) — the classifier's instruction block says the embedded prompt is
  an artifact to categorize, not instructions to follow;
- `classifier_version` lives in `prompt_versions` (`case_difficulty_classify: "v1"`); a bump
  invalidates the cache by key, forking history cleanly.

### 2.3 Allocation + dedup

`src/evals/sampler.py::sample_cases(purpose, n, frame, features, rng_seed)`:
- quota over the difficulty axis, **oversampling hard**: 25% easy / 33% moderate / 42% hard
  (downgrades die on hard cases; modal-easy sampling is why past verdicts were rosy);
- within a stratum: weighted random **without replacement**, weight = census call count
  (recurring prompt shapes matter proportionally), RNG seeded with the sweep `run_id`
  (reproducible: the same sweep re-run draws the same sample);
- underfilled strata spill to the nearest difficulty bucket (logged);
- **dedup**: by `prompt_sha256` within the sample (the loader already does this) AND against
  the previous 2 sweeps' samples for the same `(purpose, candidate)` — read back from the
  `sample_manifest` recorded in `model_eval_verdicts.summary_json` — so the rolling ledger
  accumulates *fresh* evidence instead of re-grading the same sha (fine to re-use a sha for a
  *different* candidate);
- the chosen sample (shas + strata + weights + frame_share) is written into
  `summary_json.sample_manifest` — the verdict's provenance is auditable from the DB alone
  (existing convention).

### 2.4 Sample size and the confidence gate (be honest, not fake-precise)

Per-sweep `n` by risk tier (nomination's tier, else `RISK_NOTES`):
**SAFE 8 · CANDIDATE 12 · RISKY 16**, with `min_n` raised to match (the backlog already
wants ≥8–10 for risky purposes). A single sweep at n=8–16 cannot honestly clear an 0.8
parity bar (binomial 90% CI at n=8 is ~±0.23) — and it doesn't have to: the real statistical
engine is the **standing ledger + streak rule** (`apply_model_switches`
`--consecutive-switch 3`), which pools ~3×n cases across ≥3 weeks × 2 judges before any
switch. Make that pooling explicit with one new deterministic check in
`apply_model_switches.evaluate_switches`:

```python
def wilson_lower_bound(wins_or_ties: int, n: int, z: float = 1.96) -> float:
    """95% Wilson score interval lower bound for the pooled parity proportion."""
    if n == 0:
        return 0.0
    p = wins_or_ties / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return center - half
```

**Switch gate addition:** besides the existing streak, require pooled (last 3 sweeps,
per judge, taking the MIN across judges) `wilson_lower_bound(wins_or_ties, n) >= 0.70`.
Calibration: 32/36 pooled (≈89% parity) → LB ≈ 0.75 → switches; 24/30 (80% on the nose)
→ LB ≈ 0.63 → correctly held. That is exactly the pareto philosophy: a switch needs a
candidate *clearly* at parity, not one that scraped the threshold on a small sample.

### 2.5 Data model

```sql
CREATE TABLE eval_case_features (
  purpose TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  classifier_version TEXT NOT NULL,
  ticker TEXT, scope TEXT,
  prompt_chars INTEGER NOT NULL,
  difficulty TEXT NOT NULL CHECK (difficulty IN ('easy','moderate','hard')),
  case_type TEXT NOT NULL,
  hard_signals_json TEXT NOT NULL,
  classified_at TEXT NOT NULL,            -- naive-UTC ISO
  PRIMARY KEY (purpose, prompt_sha256, classifier_version)
);
```

### 2.6 New purposes / cadence / failure modes

- `case_difficulty_classify` — operational recipe + the §1.5 hygiene touches (FAST pin,
  prompt_versions, META_PURPOSES, CAPTURE_DENYLIST, $2/mo warn budget). Runs lazily inside
  the sweep (`scope="meta_eval"`, `force_budget_bypass=False` — it's cheap; let the budget
  row govern it).
- Classifier down / unparseable (`StructuredParseError`) → **degrade to deterministic
  strata only** (length terciles + ticker cap); the sweep proceeds, the manifest records
  `difficulty='unclassified'` so the degradation is visible.
- Skewed classifier (everything "moderate") → the manifest's stratum counts surface it; the
  spot-check script (§3.6) samples classifications for manual agreement the same way judge
  verdicts are spot-checked.
- Frame too thin → `INSUFFICIENT_FRAME` (§2.1), extra harvest queued; never silently graded.

---

## 3. Subsystem 3 — per-query eval criteria (tailored checklists)

Replaces nothing — **augments** the fixed 4-facet judge with case-specific resolution.
The four facets stay: they are the stable cross-purpose backbone and keep old and new
verdicts comparable.

### 3.1 The deriver

**New purpose `query_criteria_derive`** (Sonnet — real reading comprehension over long task
prompts; latency irrelevant). New module `src/llm/query_criteria.py`.

Input: the captured TASK PROMPT (the same text production sent — truncated to ~12K chars)
plus the purpose name. **Nothing else** — no golden answer, no incumbent response, no
candidate output. Output:

```json
{"criteria": [
  {"id": "c1", "kind": "content",   "weight": 2,
   "statement": "Names >=2 non-consensus failure modes tied to KPIs the prompt supplies"},
  {"id": "c2", "kind": "format",    "weight": 1,
   "statement": "Output is a single JSON object with keys risk_title, evidence, refutation"},
  {"id": "c3", "kind": "grounding", "weight": 2,
   "statement": "Every quantitative claim cites a figure present in the supplied excerpts"},
  {"id": "c4", "kind": "constraint","weight": 1,
   "statement": "Does not exceed the prompt's 400-word cap for the summary section"}
]}
```

Derivation rules embedded in the deriver prompt (these make criteria robust without a
golden answer):
1. **Decidable from the response text alone** — a grader holding only the response (and the
   prompt) can verify each item; no "is this factually true in the world" items;
2. **Derived only from what the task prompt explicitly demands or supplies** — the deriver
   must not assert world facts absent from the prompt (that would smuggle in a pseudo-golden
   answer of unknown quality);
3. **Binary/ternary phrasing** — "names ≥2 X", "contains exactly one JSON object", never
   "is insightful";
4. 4–8 items, each tagged `kind ∈ {content, format, grounding, constraint}` + integer weight
   1–3. Parsed with `call_llm_structured`; malformed ⇒ the case runs facet-only (§3.4).

### 3.2 Reproducibility by construction

Criteria are derived **once per (purpose, prompt_sha256, criteria_version)** and cached in
`query_criteria`; every subsequent evaluation of that prompt — any candidate model, any
judge, any prompt-A/B run (§4), any week — scores against the **identical checklist**. The
deriver's temperature-driven variance therefore cannot leak into cross-run comparisons.
`criteria_version` = the deriver's `prompt_versions` entry; bumping it forks history and
invalidates the cache by key.

```sql
CREATE TABLE query_criteria (
  purpose TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  criteria_version TEXT NOT NULL,
  criteria_json TEXT NOT NULL,           -- the validated list above
  derived_by_model TEXT NOT NULL,
  derived_at TEXT NOT NULL,              -- naive-UTC ISO
  PRIMARY KEY (purpose, prompt_sha256, criteria_version)
);
```

### 3.3 Judge integration — extend, don't reinvent

`backend_judge.build_judge_prompt` gains one optional param:

```python
def build_judge_prompt(..., criteria_block: str | None = None) -> str: ...
```

When present, the judge prompt gains, between the responses and the facet instructions:

```
=== TASK-SPECIFIC CHECKLIST (derived from the task itself; judge each item) ===
c1 (content, w2): Names >=2 non-consensus failure modes tied to KPIs the prompt supplies
c2 (format, w1):  Output is a single JSON object with keys risk_title, evidence, refutation
...
For each item pick which response better satisfies it: "A", "B", or "tie".
Weigh these checklist items heavily when choosing the OVERALL winner.
```

and the output contract adds `"checklist": {"c1": "A"|"B"|"tie", ...}`.
`parse_pair_verdict` accepts the key **tolerantly** (absent ⇒ legacy behavior; present but
malformed ⇒ that pass fails closed exactly like a bad facet — the existing contract).
`JudgedPair` gains `checklist_winners: dict[str, str] | None = None`, consolidated across
the position-swapped passes with the same agree-or-tie rule as `facet_winners`.

**Deliberately unchanged:** the winner/margin consolidation math, position-swap, dual-judge,
brand-blind labels. The checklist shapes the judge's overall call *in context* and lands
item-by-item in the audit trail (`summary_json.cases[*].checklist`), but the harness's
deterministic protocol is untouched — protocol stability is what keeps verdict history
comparable.

`model_eval.judge_case` threads `criteria_block` through (looked up from `query_criteria`
by the case's `prompt_sha256`); `run_model_eval_sweep._evaluate_candidate` derives-or-loads
criteria for each sampled case before judging.

### 3.4 The anti-leak rule (constraint for §5)

Criteria exist **only** in the `query_criteria` table and **only** enter *judge* prompts.
The generating call (`model_eval.run_model`) replays `case.prompt` byte-identically — the
checklist never appears in, is never appended to, and never wraps, any prompt sent under a
non-judge purpose. Guard test: monkeypatched `call_llm` in the harness tests asserts
`sha256(sent_prompt) == case-record prompt_sha256` for every generation call, and asserts no
generation prompt contains the literal `TASK-SPECIFIC CHECKLIST` sentinel.

### 3.5 Quality-of-criteria telemetry

Per sweep, record in `summary_json`:
- `checklist_discrimination`: fraction of checklist items that ever resolved non-tie across
  the purpose's cases (persistently ~0 ⇒ criteria too generic — tighten the deriver prompt);
- `criteria_missing`: cases that ran facet-only (deriver failed) — a freshness alarm input.

Extend `execution/spot_check_eval_judge.py` (or a sibling
`execution/spot_check_criteria.py`): sample N derived checklists for manual review — "is
each item actually entailed by the prompt?" — recorded as `manual:criteria_spot_check`
rows, mirroring the judge-agreement spot-check that already exists (and has never been run —
running both together is cheap; see backlog item).

### 3.6 New purposes / cadence / failure modes

- `query_criteria_derive` — operational recipe + §1.5 hygiene (Sonnet pin, prompt_versions,
  META_PURPOSES, CAPTURE_DENYLIST, $5/mo warn budget). Runs lazily during sweeps/A-B runs
  (`scope="meta_eval"`); with caching, steady-state ≈ new-distinct-cases-per-week × ~$0.04.
- Deriver down → facet-only judging (flagged, never blocks a sweep).
- Deriver hallucination of pseudo-golden facts → rule 2 + the spot-check; a checklist item
  the prompt doesn't entail shows up there.
- Criteria gamed by a variant (§4): impossible by construction — §4 derives criteria from
  the *baseline* prompt, and criteria are cached before any variant exists.

---

## 4. Subsystem 4 — automated prompt A/B testing

Goal: improve the platform's own prompts, not just swap models. The existing pieces this
composes: `prompt_versions.py` (the version dimension), the prompt-change workflow in
`llm_calls.md`, `summarize_by_prompt_version` (the read side), and `judge_pair` (the
comparator).

### 4.1 The variant model — deterministic edit-splice on rendered prompts

Production prompts are RENDERED (instruction scaffold + per-ticker data). The captured
corpus holds rendered artifacts. So a variant is defined as a **deterministic textual
transformation of the rendered prompt**: an ordered list of exact-match edits on the
instruction scaffold — the parts identical across all captured renders of that purpose.

```python
@dataclass(frozen=True, slots=True)
class PromptEdit:
    find: str      # must occur EXACTLY ONCE in every sampled rendered prompt
    replace: str

def apply_edits(prompt: str, edits: tuple[PromptEdit, ...]) -> str:
    out = prompt
    for e in edits:
        if out.count(e.find) != 1:
            raise EditAnchorError(e.find[:80])   # reject BEFORE any LLM spend
        out = out.replace(e.find, e.replace, 1)
    return out
```

Invariant (tested): `variant_prompt` differs from `baseline_prompt` by exactly the intended
edits — a unified diff of the two must be fully covered by the edit list. The embedded
per-ticker data is byte-identical by construction. This is the §5 isolation guarantee for
A/B: *the only delta is the intended prompt change; never harness bookkeeping.*

### 4.2 Variant generation (`prompt_variant_propose`)

**New purpose `prompt_variant_propose`** (Opus — prompt engineering is judgment-tier).
Input:
- the CURRENT canonical prompt **template** (the checked-in module-level constant — prompts
  are greppable constants per `llm_calls.md`; the runner is pointed at the constant via a
  small per-purpose registry entry),
- one rendered example from the frame (so the proposer sees scaffold vs data),
- the purpose's rubric (`evals/rubrics/<p>.md`) if one exists,
- the improvement signal: recent judge rationales where this purpose's incumbent LOST a
  facet (from `model_eval_verdicts.summary_json`), recent eval failures
  (`eval_case_results`), and format-retry counts.

Output (structured, validated):

```json
{"hypothesis": "moving the output schema to the end + an explicit counting rule cuts format misses",
 "edits": [{"find": "<exact substring of the template>", "replace": "<replacement>"}],
 "expected_effect": "format facet + c2-style checklist items"}
```

Validation before a cent is spent: every `find` anchors exactly once in the template AND in
**every** sampled rendered prompt (§4.1) — edits that touch the data region can't anchor
uniquely across renders and are rejected (`status='rejected_anchor'`). Semantic guard: the
proposer is instructed the variant must preserve WHAT is asked (same task, same output
consumer) — a variant that changes task semantics is a product change, out of scope; §3's
baseline-derived criteria then penalize it correctly if it slips through.

### 4.3 Experiment execution

`execution/run_prompt_ab.py --purpose <p> [--experiment <id>] --repo-root <MAIN>`:

1. Freeze `model = _model_for(purpose)` into the experiment row (a mid-experiment model
   switch must not confound the comparison).
2. Sample cases via §2's sampler (same strata, same seeding; n = 12 default).
3. **Baseline side**: reuse the captured incumbent response IF the capture's `model` equals
   the frozen model (zero extra spend — the `model_eval` reuse trick); else re-run the
   baseline prompt fresh under the frozen model, `scope="prompt_ab"`.
4. **Variant side**: `apply_edits(case.prompt, edits)` → run under the SAME frozen model,
   `scope="prompt_ab"`, capture OFF (orchestrator pops `LLM_CAPTURE_DIR`, the
   `run_weekly_model_eval` precedent), `force_budget_bypass=True` (measurement, not
   production — the `run_model` precedent).
5. Judge with `judge_pair`: baseline output → slot A, variant output → slot B (position-swap
   randomizes presentation anyway), dual judge, plus §3 criteria **derived from the baseline
   prompt** (cached; the task intent is the baseline's — see §3.6 note on gaming).
   The judge's `task_prompt` context = the **baseline** prompt: the judge measures "which
   output better serves the task"; the baseline defines the task. Judges never see the
   edits, the hypothesis, or which side is the variant.
6. Verdict via a thin wrapper over `decide_switch`-style tallies (reuse the math; relabel):
   `PROMOTE_VARIANT | KEEP_BASELINE | HOLD | INSUFFICIENT_DATA | VARIANT_ERRORED`
   (`VARIANT_ERRORED` mirrors `CANDIDATE_ERRORED` — a variant that breaks the model
   operationally ≥50% is an authoring failure, not a quality verdict).

### 4.4 Promotion criteria and the promotion act

Promotion bar (asymmetric to the model-switch bar — a prompt change is cheap and
git-reversible, but churn has cost, and "provably better" still rules):
- variant **strictly wins ≥60%** of judged cases AND baseline strictly wins **≤20%**
  (zero-regression guard), per judge, both judges agreeing (cross-judge ≥0.6);
- across **≥2 experiment runs on ≥10 distinct cases total** (fresh samples — §2 dedup);
- the purpose's existing eval gate passes under the variant: golden set / rubric run via
  `run_llm_evals.py --purpose <p> --min-score <bar>` with the variant applied locally.

Promotion is a **human PR**, not an auto-apply (v1 decision; revisit as open question Q1):
the experiment row supplies the exact template edits + the evidence; the PR applies the
edits to the real prompt constant and bumps `prompt_versions` (`v2`→`v3` …) per the existing
prompt-change workflow. This keeps `git` the source of truth for prompt text and reuses the
whole calibration read side unchanged.

### 4.5 Data model

```sql
CREATE TABLE prompt_experiments (
  id INTEGER PRIMARY KEY,
  experiment_id TEXT NOT NULL UNIQUE,     -- uuid4 hex
  purpose TEXT NOT NULL,
  baseline_prompt_version TEXT NOT NULL,  -- prompt_versions at proposal time
  variant_label TEXT NOT NULL,            -- 'exp-<8hex>'
  hypothesis TEXT NOT NULL,
  edits_json TEXT NOT NULL,               -- ordered [{find, replace}]
  frozen_model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','rejected_anchor','running','decided','promoted','abandoned')),
  decision TEXT,                          -- PROMOTE_VARIANT | KEEP_BASELINE | HOLD | ...
  created_at TEXT NOT NULL, decided_at TEXT, notes TEXT
);

CREATE TABLE prompt_ab_verdicts (         -- deliberately parallel to model_eval_verdicts,
  id INTEGER PRIMARY KEY,                 -- NOT merged into it: candidate-model semantics
  experiment_id TEXT NOT NULL,            -- and variant semantics must not pollute each other
  purpose TEXT NOT NULL,
  run_id TEXT NOT NULL,
  n_cases INTEGER, variant_wins INTEGER, baseline_wins INTEGER, ties INTEGER,
  win_rate REAL, judge_agreement REAL,
  recommendation TEXT NOT NULL,
  reason TEXT NOT NULL,
  summary_json TEXT,                      -- per-case audit + sample_manifest + checklist detail
  recorded_at TEXT NOT NULL
);
```

### 4.6 New purposes / cadence / failure modes

- `prompt_variant_propose` — operational recipe + §1.5 hygiene (Opus pin, prompt_versions,
  META_PURPOSES, CAPTURE_DENYLIST, $5/mo warn budget). Judging reuses
  `backend_compare_judge` (same judge purpose; the ledger disambiguates via
  `scope="prompt_ab"` + the experiment `run_id`).
- Cadence: on-demand + nominated — §1's nominator emits `kind='prompt_experiment'` rows for
  purposes where the *incumbent keeps winning on substance but bleeding format/conciseness
  facets* (visible in `facet_gemini_loss` aggregates) or where eval scores are mediocre; web
  purposes (`recent_developments`) are A/B-eligible even though downgrade-ineligible. One
  live experiment per purpose at a time.
- Failure modes: edits fail to anchor (rejected pre-spend); variant changes semantics
  (criteria-from-baseline + judges penalize; falls out as KEEP_BASELINE); captured baseline
  from a different model (detected via capture record's `model`, side re-run fresh);
  experiment/model-switch race (frozen_model pins the comparison; if `_model_for` moved
  mid-experiment the promotion PR must re-run the eval gate under the new model).

---

## 4.7 The randomized cycle (built 2026-07-24)

§4.1–§4.6 specified the mechanism but left the loop hand-driven, and it did not
run: `prompt_experiments` held **zero rows** from the PR5 merge (2026-07-02) to
2026-07-24. Three gaps, each closed below.

### 4.7.1 Scaffold derivation replaces the template registry (amends §4.1/§4.2)

§4.2 sourced the prompt template from "a small per-purpose registry entry"
pointing at a checked-in constant. Most purposes here build their prompt inline,
so there is no constant to point at, and any registry drifts from the code the
moment a prompt is edited.

`src/llm/prompt_scaffold.py` derives the scaffold from the **captured renders**:
lines occurring exactly once in every render, glued into contiguous blocks, each
re-verified as a once-only substring. Derived from what production actually
sent, so it cannot drift.

The derivation is also the **anchor space**. §4.1's exactly-once rule is the
admission test for a block, so a proposer restricted to quoting blocks proposes
legally by construction — "propose, then reject on anchor failure" becomes
"propose within bounds". An edit anchoring outside the scaffold is refused even
when it would splice cleanly today, because it may sit in the data region and
mutate real data on a later render.

Measured on the live corpus (2026-07-24): eligible for **9 of 9** captured
purposes, coverage 0.03–0.89 of the render, derivation 0.01s total.
A purpose with fewer than two renders, or no shared structure, is **ineligible**
and skipped loudly — never silently degraded to a weaker anchor rule.

### 4.7.2 Randomized exploration (extends §4.2)

§4.2's single Opus proposal has no exploration: one model's priors, re-proposed
every cycle, with no memory of what has already lost.

* `src/llm/prompt_strategies.py` defines eleven named **edit strategies**
  (output contract, specificity forcing, negative example, reasoning chain,
  length budget, format precision, instruction priority, consumer framing,
  self-check, scope guard, ordering swap). The drawn strategy enters the
  proposal prompt as a required direction.
* Strategies are drawn by **Thompson sampling** over Beta posteriors on "does
  this kind of edit get promoted?", pooled across purposes (per-purpose
  posteriors would need dozens of experiments each to say anything).
* Plus an **ε = 0.15 exploration floor**. Thompson alone drives a losing
  strategy to zero draws at top-k-of-eleven selection, and a strategy that is
  never drawn can never update. These posteriors go stale *by design*: every
  promotion rewrites the scaffold that later experiments edit, so "output-
  contract edits don't help" can be true in June and false in August.
* Only **decided** outcomes update a posterior. PROMOTE is a win, KEEP_BASELINE
  a loss; HOLD / INSUFFICIENT_DATA / VARIANT_ERRORED / TRANSPORT_DEGRADED update
  nothing — the existing STREAK_NEUTRAL convention.
* Every draw is **seeded** and the seed is stored on the experiment
  (`rng_seed`), so any cycle replays exactly.

`src/llm/prompt_signal.py` builds the §4.2 improvement signal that was specified
but never implemented — the shipped code passed the literal string
`"(operator-initiated; see recent verdict rationales)"`. It reads eval scores
and failures, judge rationales from cases the incumbent lost, and the
operational error rate, and yields a **deficit** in [0,1].

Two honesty rules, both load-bearing:

* A purpose with **no eval coverage** gets an explicit neutral prior (0.35) and
  `has_eval_coverage=False`, never a zero. Scoring it 0.0 would read as "this
  prompt is perfect" and would starve exactly the purposes nobody has measured.
* Rows whose **judge crashed** are excluded from the quality math and counted
  separately. Measured impact 2026-07-24: including them put `bear_case` at avg
  0.706 / 26% fail and `transcript_summary` at 0.642. Excluding them: **0.959**
  and **0.963**. The unfiltered signal would have aimed every cycle at whichever
  purpose the CLI happened to fail on most.

Purpose selection is weighted by `ab_leverage = cost_30d × (1 + 2·deficit)` —
explicitly **not** `PurposeWorkload.headroom_usd_30d`, which measures the saving
from a cheaper *model* and is 0 for web-scoped purposes that §4.6 declares
A/B-eligible.

### 4.7.3 Multi-arm experiments and combinations (extends §4.3–§4.5)

§4.5's schema modelled exactly two arms. Migration **0202** (`0202_prompt_ab_arms` — renumbered from 0200 in #1010 after a two-head collision with #1002's 0201) adds `prompt_arms`
(baseline stays implicit), `prompt_ab_verdicts.arm_label` (nullable — NULL is
the legacy-two-arm marker), and `cycle_id` / `rng_seed` / `signal_json` on
`prompt_experiments`.

* **Parallel arms** share one case sample and one baseline run per case, which
  is what makes k arms affordable and removes the sample-to-sample variance that
  makes separate experiments incomparable.
* A **composed arm** carries the union of two arms' edits. Two edits that each
  win alone can lose together; with two-arm experiments that is invisible — both
  promote, both apply, and the regression surfaces later as an unexplained
  quality drop. Composition is refused pre-spend when the edit sets overlap or
  when a dry-run splice fails (one edit's *replacement* can destroy the other's
  anchor even when the `find` strings do not overlap).
* New verdict **`INTERACTION_NEGATIVE`**: a composed arm whose win rate falls
  below its best component. Distinct from KEEP_BASELINE, which would wrongly
  imply the components were bad. It disqualifies the combination from promotion
  while leaving each component's own record intact.
* Arms promote **independently** (`--arm`), one active override per purpose.
  Auto-demote now requires **every** arm to conclude KEEP_BASELINE — one losing
  arm says nothing about an override won by a different one.

### 4.7.4 `TRANSPORT_DEGRADED` — new, and not optional (amends §4.3 step 6)

§4.3's `VARIANT_ERRORED` blames the variant when ≥50% of its cases error. That
rule assumes a healthy transport. Measured 2026-07-24 from `llm_calls`:

| month | calls | errored | rate |
|---|---|---|---|
| 2026-05 | 223 | 8 | 4% |
| 2026-06 | 4,473 | 466 | 10% |
| 2026-07 | 10,340 | 7,488 | **72%** |

Under that regime the old rule condemns healthy variants as authoring failures
and teaches the bandit to avoid whichever strategy was unlucky. `decide_ab` now
also takes `n_baseline_errors`; when the **unedited** baseline is failing at
≥25%, the run is `TRANSPORT_DEGRADED` — neutral for the bandit and for the
pooled promotion bar, because nothing was measured about the prompt.

### 4.7.5 Cadence and budget (settles §4.6)

Prompt cycles run as **step 4 of `run_weekly_model_eval.py`**, riding the
existing weekly registration rather than adding a schedule — the scheduling
directive's rule for any LLM leg, and it keeps measurement bursts inside one
already-protected window.

Owner ceiling (2026-07-24): **2–3 cycles/week within $40/month**. The real
governor is the month-to-date measurement spend checked before every cycle, not
the cycle count — a week whose proposals fail cheaply should not consume the
budget of a week where every arm ran.

`_SELF_PURPOSES` bars the loop from A/B-ing the optimizer's own prompts (I5).

The purpose draw is restricted to purposes the capture corpus can actually
scaffold (≥2 distinct captured renders) — found on the first prod dry-run
(2026-07-25), where the unrestricted draw burned its cycle on `key_metrics`
(high leverage, zero captures). Excluded purposes are logged as a harvest hint
(#1014); an uncaptured purpose's fix is more harvest, not a wasted draw.

Companion incident work (#1008, outside this directive but load-bearing for
it): the transport itself now classifies CLI failures (`llm/transport.py`,
`[class]`-prefixed ledger errors), circuit-breaks on quota exhaustion
(`LLMQuotaExhausted` — eval/judge callers ABORT, production defers per-item),
and the model loop gained `JUDGE_DEGRADED` (streak-neutral) because errored
judge_pairs previously landed as ties = parity — an outage could have built a
SWITCH_DOWN streak.

Live verification (2026-07-25, prod dry-run): drew `valuation_basis` on
evidence (10 judge rationales, deficit 0.372), derived a 10-block scaffold at
89% coverage, produced two legally-anchored Opus proposals under drawn
strategies plus a composed combination arm. Meta spend at that point:
$9.39 / $40 MTD.

### 4.7.6 Still open

* The nominator's `kind='prompt_experiment'` rows are still not consumed by the
  cycle (it draws by leverage×deficit instead). Eight nominations exist to date,
  all `model_downgrade`; the nominator has never emitted a prompt experiment.
* No Evals-panel surface for arms or the strategy posteriors yet — the loop is
  auditable from the DB alone (`prompt_arms` ⋈ `prompt_ab_verdicts`).
* Composition is limited to pairs of arms **within one cycle**. Composing
  against previously-promoted overrides across cycles is the natural next step
  and needs a rule for stacking two active overrides.
* Harvest coverage: 50 of ~59 costed production purposes had no scaffoldable
  captures on 2026-07-25 (incl. `news_structuring`, `saydo_commitment_extract`
  by cost) — widening `_HARVEST_STEPS` in `run_weekly_model_eval.py` is the
  highest-leverage next move for both this loop and the model loop.

---

## 5. Subsystem 5 — the isolation contract (anti-bias; load-bearing)

The owner's hardest requirement: **the generating call must be blind to the harness.** When
any subsystem above causes a response to be generated, the prompt bytes must be exactly what
production would send — no eval metadata, no "you are being tested" framing, no injected
criteria, no experiment ids — and the transport must be the production transport. Otherwise
the measurement contaminates the measured (and, via the Claude CLI's context-loading
behavior, biases it mechanically, not just psychologically).

### 5.1 Precedents already in the codebase (this contract names + extends them)

| Precedent | Where | What it isolates |
|---|---|---|
| Neutral-cwd subprocess | `src/llm/cli.py::_neutral_subprocess_cwd` (every `claude -p` runs from an empty temp dir) | No project `.mcp.json` / project context boots into the generation call — production AND replay get the same context-free transport |
| (Retired twin) gemini-cli neutral-cwd context suppression | `directives/gemini_backend.md` migration notes — the bare `generate_content()` API "carries no implicit context by construction" | Same guarantee, achieved structurally after the CLI was dropped |
| Brand-blind judge labels | `src/llm/backend_judge.py` — "Response A"/"Response B", never model names; position-swap; dual judge | Judge priors can't attach to brands; position bias filtered |
| Capture denylist | `src/llm/capture.py::CAPTURE_DENYLIST` (`backend_compare_judge`, `eval_judge`) | Grader traffic never re-enters the harvest corpus it consumes |
| Capture OFF during sweeps | `run_weekly_model_eval.py` pops `LLM_CAPTURE_DIR`; `model_eval.py` docstring rule | Eval traffic never becomes next week's "production sample" |
| Out-of-band attribution | `scope="model_eval"` / `run_id` are `record_llm_call` ledger columns, never prompt content | Bookkeeping travels beside the call, not inside it |

### 5.2 The contract — seven testable invariants

- **I1 — Byte-identity of generation prompts.** Any generation call made by eval machinery
  sends prompt bytes identical to a production prompt: replay = `case.prompt` verbatim
  (`run_model` already passes it straight through — keep it that way); prompt-A/B variant =
  `apply_edits(baseline)` where the edit list is the *entire* intended change (§4.1
  diff-coverage invariant). No wrappers, no `[EVAL]` markers, no criteria, no experiment
  labels. *Test:* harness tests with a monkeypatched `call_llm` assert
  `sha256(sent) == recorded prompt_sha256` (replay) and `sent == apply_edits(baseline)`
  (A/B).
- **I2 — Out-of-band bookkeeping only.** Experiment identity lives in ledger columns
  (`purpose`, `scope`, `run_id`) and the harness tables (`model_eval_verdicts`,
  `optimizer_nominations`, `eval_case_features`, `query_criteria`, `prompt_experiments`,
  `prompt_ab_verdicts`) — never in prompt text. *Test:* a source-level guard (the
  raw-hex-guard pattern from the design-token work) asserting no code path under `src/llm/`
  or `src/evals/` interpolates `scope`/`run_id`/experiment ids into a prompt string.
- **I3 — Production transport, exactly.** Replays go through the same `call_llm` →
  `_call_claude` (or backend) path as production: same neutral cwd, same CLI flags, same
  JSON envelope, no extra `--append-system-prompt`/system-prompt flags ever added for eval
  runs. An explicit `model=` on a candidate replay is precisely what production would send
  if the pin changed — that is the *only* permitted delta (plus `force_budget_bypass`,
  which affects whether the call happens, never its content). *Test:* assert the subprocess
  arg-list built for a replayed call equals the production arg-list modulo `--model` value.
- **I4 — Judge/meta quarantine.** All meta-machinery purposes (`optimizer_nominator`,
  `case_difficulty_classify`, `query_criteria_derive`, `prompt_variant_propose`, plus the
  existing judges) are in `CAPTURE_DENYLIST` and carry `scope="meta_eval"` (or the existing
  judge scopes) — so they never enter a harvest corpus and never inflate workload cost
  (§0 scope taxonomy). *Test:* `test_meta_purposes_in_capture_denylist` asserts the frozen
  sets stay superset-consistent.
- **I5 — No self-observation loops.** Capture is OFF in any process running evals
  (assert + pop `LLM_CAPTURE_DIR`, the existing precedent); the inventory excludes
  `EVAL_SCOPES`; meta purposes are excluded from the nominable universe; replay/variant
  outputs are never written to production artifact stores (`llm_artifacts` / disk sidecars)
  — `run_model` doesn't write artifacts today; the A/B runner must not either.
- **I6 — Blindness at grade time.** Judges see Response A / Response B, the task prompt
  (baseline, for A/B), and the §3 checklist — never model names, never variant labels,
  never "candidate"/"downgrade"/"experiment" framing. Position-swap remains mandatory for
  every pair. (The checklist is derived from the task prompt alone — §3.1 — so it cannot
  smuggle side information about either response.)
- **I7 — Asymmetric knowledge is fine downstream, never upstream.** The DECISION layer
  (`decide_switch`, `apply_model_switches`, promotion PRs) may know everything; the
  GENERATION layer knows nothing; the JUDGE layer knows only task + outputs + checklist.
  Any new feature that needs the generator to "know" something about the eval is, by this
  contract, mis-designed — move that knowledge to the judge or the decision layer.

### 5.3 Why this is load-bearing (the mechanism, stated once)

Two distinct contamination channels, both closed:
- **Content channel:** anything appended to the prompt ("this is an eval", criteria, run
  ids) changes model behavior — models demonstrably shift style/effort under test framing —
  so the measured output is not the production output. I1/I2 close it.
- **Transport channel:** the Claude CLI loads context by cwd (project `.mcp.json`,
  CLAUDE.md-class files); a bespoke "eval client" or a different working directory would
  inject different implicit context than production. I3 closes it by *reusing* the
  production entry point and its `_neutral_subprocess_cwd` — the reason replays must never
  grow their own subprocess wrapper.

---

## 6. Cross-cutting: OpenRouter / N-backend integration

The third backend is in flight **in this worktree right now** (`src/llm/openrouter_backend.py`
+ ladder changes, uncommitted at design time): `model_ladder` gains the `OPENROUTER` family
with slug ids (`deepseek/deepseek-chat`, `qwen/qwen-2.5-72b-instruct`, …), and
`cheaper_candidates(..., include_openrouter=False)` makes the pool **opt-in** — its own
docstring reserves the opt-in act for "once the meta-eval nominates it". This directive is
that meta-eval. Integration rules:

- **The ladder stays the only model registry.** OpenRouter candidates are ordinary
  `ModelCost` entries; the inventory, nominator (closed-vocabulary over the ladder), sampler
  and judge machinery need **zero structural changes**. The nominator receives the FULL
  ladder including OpenRouter rows; a validated nomination is the opt-in that flips
  `include_openrouter=True` for that (purpose, candidate) pair at sweep time (§1.2, §1.4).
  Unnominated sweeps keep the default-off posture — the automatic loop can never flood a
  purpose with untested open-weight models.
- **Seed prices are rank-only.** The OpenRouter ladder prices are seed approximations; the
  backend records the REAL charged cost per call (`usage.cost`) into
  `llm_calls.cost_estimate_usd`. The inventory's leverage score reads the ledger (real),
  the ladder only orders candidates (approximate) — that split is already correct; the
  nominator prompt should note candidate prices are indicative.
- **Data governance is the backend's job, not the ladder's.** The backend pins
  `provider.data_collection="deny"` by default (prompts never route to providers that train
  on them) and pins provider/quantization so a graded candidate is a stable, reproducible
  thing (its "model-identity guardrail" — which is itself an eval-integrity requirement:
  the parity verdict transfers to production because eval and production calls share one
  routing config). Replay-based evals inherit both guarantees by calling through
  `call_llm`; do NOT duplicate a `data_policy_ok` flag in the ladder. Residual owner
  question: is deny-by-default sufficient for replaying real thesis/IR prompts (Q4)?
- **Judge pool by family, not by name.** `run_model_eval_sweep._judge_model_for` currently
  hardcodes claude→Opus / gemini→Pro; replace with a `JUDGE_POOL: dict[str, str]`
  (family → judge model id) in `model_ladder.py`. Rules: judges for any verdict come from
  **≥2 distinct families**, and OpenRouter open-weight models are candidates, NOT judges,
  until a judge-agreement spot-check certifies one (judging needs the discriminating model
  to out-class both contestants — the `backend_compare_judge` Opus rationale). The
  `CLAUDE`/`GEMINI` constants in `backend_judge` are SLOT names semantically
  (`model_eval.py` documents `INCUMBENT_SIDE`/`CANDIDATE_SIDE`); renaming them is optional
  cleanup, not required.
- **Flaky new backends are already handled** — `CANDIDATE_ERRORED`
  (`CANDIDATE_ERROR_RATE_THRESHOLD = 0.5`, #723) books OpenRouter teething failures as
  infra, not quality, exactly as it did for the Gemini OAuth shutdown. That guard is what
  makes aggressive candidate-pool widening safe.

---

## 7. Incremental build order (smallest-valuable-first; one PR per phase)

Repo conventions apply to every PR: pyright strict + ruff on touched files only,
`cast("dict[str, object]", …)` at JSON boundaries, `default_factory=list[T]`, naive-UTC
stamps, best-effort ledger/telemetry writes, tests monkeypatch all LLM calls, migration
number + down_revision picked at rebase time (head `0131` as of 2026-07-02).

| PR | Scope | Why first / value |
|---|---|---|
| **1** | `src/llm/workload_inventory.py` + `execution/report_workload_inventory.py` + `RISK_NOTES` + `EVAL_SCOPES` constant. Pure read; NO migration, NO LLM. | Immediately replaces the hand-maintained cost/risk tables; the leverage rank alone is a better sweep-targeting input than "all active purposes". Everything later consumes it. |
| **2** | Stratified sampler: migration (`eval_case_features`), `case_difficulty_classify` purpose (+ §1.5 hygiene), `src/evals/sampler.py`, swap `_load_cases_from_files` → `sample_cases` in `run_model_eval_sweep`, per-tier n, `INSUFFICIENT_FRAME`, `sample_manifest` in `summary_json`, `wilson_lower_bound` gate in `apply_model_switches`. | The highest-leverage fix to verdict QUALITY — every existing sweep improves even before nominations/criteria exist. Unblocks honest switches (the empty `model_pin_overrides` problem). |
| **3** | Nominator: migration (`optimizer_nominations` + budget seed), `optimizer_nominator` purpose (+ hygiene), validation + deterministic fallback, `--from-nominations` in the sweep, step 0 in `run_weekly_model_eval`, `ladder_sha` trigger. | Converts PR 1's rank into the standing "what to test next" feed; kills the static list. |
| **4** | Per-query criteria: migration (`query_criteria` + budget seed), `query_criteria_derive` purpose (+ hygiene), `build_judge_prompt`/`parse_pair_verdict`/`JudgedPair` extension, sweep threading, discrimination telemetry, criteria spot-check script. | Sharpens every judge verdict (model sweeps AND the A/B harness about to land) — build before A/B so experiments grade against tailored criteria from day one. |
| **5** | Prompt A/B: migration (`prompt_experiments` + `prompt_ab_verdicts` + budget seed), `prompt_variant_propose` purpose (+ hygiene), `apply_edits` + anchor validation, `execution/run_prompt_ab.py`, promotion-workflow section appended to `directives/llm_calls.md`, nominator emits `kind='prompt_experiment'`. | The new capability (self-improving prompts) — lands on top of sampler + criteria + nominations. |
| **6** | Isolation consolidation + surface: `tests/test_meta_eval_isolation.py` (the I1–I7 guard suite in one place; each earlier PR ships its own local guards too), Optimizer-panel extension (`src/pipeline/model_eval_panel.py`): pending nominations view, frame_share / INSUFFICIENT_FRAME chips, experiment status + promotion evidence, meta-machinery $/mo line; freshness alarm (no nomination run in 45d / no sweep verdict in 14d). | Makes the contract enforceable-forever and the whole loop visible where the owner already looks (the panel is the audit surface precedent, PR4 2026-07-01). |

Explicit non-goals (do-not-re-propose hygiene): no live `call_llm` sampling hook (owner
decision 2026-06-11 — scheduled batch stands); no CI-run LLM evals (forbidden by design);
no auto-apply of prompt text in v1 (human PR promotes); no new pairwise-judge protocol (the
brand-blind/position-swap/dual-judge harness is reused everywhere); no merging of
`prompt_ab_verdicts` into `model_eval_verdicts` (verdict semantics stay separate).

---

## 8. What the machinery itself costs (bounded, attributed, budget-rowed)

| Purpose | Model | Volume | Est. $/mo |
|---|---|---|---|
| `optimizer_nominator` | Opus | ~1–2 calls/mo (~12K in / 2K out) | ~$0.60 |
| `case_difficulty_classify` | Haiku/FAST | ~40–80 new distinct prompts/wk, cached forever | ~$0.50 |
| `query_criteria_derive` | Sonnet | ~30–50 new cases/wk, cached forever | ~$2–6 |
| `prompt_variant_propose` | Opus | ~2–4 experiments/mo | ~$1–2 |
| Judge overhead from checklists | (existing `backend_compare_judge` line) | +10–20% judge tokens | ~$5–9 |
| A/B generation runs | frozen incumbent model | ~12 cases × 1–2 sides × 2–4 exp/mo | ~$2–8 |

Total ≈ **$11–26/mo**, all under `EVAL_SCOPES`, each with its own warn-mode budget row —
against identified headroom of >$150/mo (`news_structuring` Opus→Sonnet alone ≈ $40–50/mo
per the backlog) plus whatever the widened OpenRouter pool unlocks.

---

## 9. Open questions for the owner

1. **Prompt-variant auto-apply (v2)?** v1 promotes via human PR. Once ≥3 experiments have
   promoted cleanly, should a `prompt_pin_overrides`-style mechanism apply winning edits
   automatically (mirroring `model_pin_overrides`, reversible, auto-demote on regression)?
2. **Nominator authority ceiling.** May the nominator *exclude* purposes (negative
   nominations: "stop testing X, three KEEP streaks and no headroom"), or only rank?
   Recommended: rank-only; the deterministic rotation still covers everything eventually,
   so nothing can be silently starved by a bad nomination.
3. **RISKY-tier hard floor.** Keep `advisor_*` / `valuation_basis` /
   `kpi_registry_auto_proposal` / `material_news_classification` merely at higher bars
   (min_n 16 + Wilson gate), or add a `NEVER_AUTO_SWITCH` frozenset that always requires a
   human PR regardless of evidence? Recommended: the frozenset — the blast radius of a bad
   switch there is silent portfolio harm.
4. **OpenRouter data policy.** Replay-based evals send real production prompts (thesis/IR
   content) to third-party providers. The in-flight backend already pins
   `provider.data_collection="deny"` — is that sufficient for replaying real thesis
   prompts, or should replay-based evals additionally require a per-model owner review
   before first nomination? (Golden-set testing works either way — checked-in synthetic
   data.)
5. **Judge pool at ≥3 families.** Stay with 2 judges from 2 families (cheapest adequate), or
   move to 3-family majority voting for RISKY-tier verdicts only (≈+50% judge cost on a
   small slice)?
6. **Residual `purpose=NULL` traffic** ($149/30d trailing window, mostly pre-#722): the
   inventory can only govern named purposes. After the SayDo fix's trailing window clears,
   should `call_llm(purpose=None)` become a hard deprecation (raise in dev, warn in prod)
   so the workload universe is complete by construction?

---

## 10. Owner decisions — LOCKED 2026-07-02

Owner (Bhanu) reviewed §9 and the surrounding design on 2026-07-02. The decisions
below are authoritative; **where a decision conflicts with a recommendation earlier
in this doc, §10 wins.** Several diverge from the recommendation on purpose.

**Q1 — Prompt-variant auto-apply: BUILD NOW** (overrides §4.4's human-PR-only v1).
PR5 ships a `prompt_pin_overrides` table (purpose → ordered edits, `active`, reversible)
mirroring `model_pin_overrides`: a cleanly-promoted A/B variant auto-applies at render
time and auto-demotes on a later regression. Because production prompt text now diverges
from the checked-in constant, PR5 MUST also ship (a) a **git-reconciliation trail** — every
auto-apply logs the exact edits + `experiment_id` + a ready-to-paste diff so the checked-in
constant can be caught up in a routine PR; (b) full visibility of the active pin in the
Optimizer panel; (c) the same reversibility/lock affordances `model_pin_overrides` has. The
isolation contract is unchanged: the sent prompt is `apply_edits(baseline)` byte-for-byte
(I1); no experiment metadata ever enters a generation prompt (I2).

**Q2 — Nominator authority: EXCLUSIONS ALLOWED** (overrides §9-Q2 / §1.2 "rank-only").
The nominator may emit negative nominations (`kind='exclude'` — a purpose it judges settled:
"three KEEP streaks, no headroom"). Mandatory anti-starvation guards so a bad exclusion
self-heals and never silently freezes measurement forever:
- every exclusion carries a TTL (`expires_at`, default 60d) after which the purpose
  re-enters the nominable universe automatically;
- a deterministic rotation floor still sweeps every non-excluded purpose on cadence, and any
  purpose not swept in `MAX_UNSWEPT_DAYS` (default 90d) is force-re-included regardless of an
  active exclusion;
- exclusions are visible + one-click-reversible in the Optimizer panel;
- closed-vocabulary validation applies to exclusions too (unknown purpose → dropped+logged).
`optimizer_nominations.kind` CHECK extends to include `'exclude'`; add `expires_at TEXT`
(naive-UTC, nullable).

**Q3 — RISKY floor: HIGHER BARS, NO frozenset — plus first-class auditability + remediation**
(overrides §9-Q3's recommended `NEVER_AUTO_SWITCH`). `advisor_*` / `valuation_basis` /
`kpi_registry_auto_proposal` / `material_news_classification` stay auto-switchable at the
RISKY bar (min_n 16 + Wilson LB ≥0.70 + streak + 3-family judging per Q5a). NO
`NEVER_AUTO_SWITCH` frozenset. In exchange, build a first-class audit + remediation path for
every RISKY auto-switch:
- a switch-audit record (new `model_switch_audit` table, or `model_pin_overrides` history)
  capturing the full pooled evidence that justified the switch — per-judge tallies, Wilson
  LB, the `sample_manifest`, the verdict ids, who/what/when, and the `ladder_sha`;
- a loud RISKY-tagged switch alert (the existing `apply_model_switches` alert path);
- fast remediation: auto-demote on the next regressing sweep (already designed) PLUS a
  one-command manual revert + lock, surfaced in the panel;
- dedicated tests for the RISKY-tier switch → demote → manual-revert path.

**Q4 — OpenRouter data policy: DENY-DEFAULT SUFFICIENT** (confirms §6 / §9-Q4). No per-model
owner-review gate. Any ladder OpenRouter model validated by `cheaper_candidates` is nominable
for real-prompt replay; the backend's provider-pinned `data_collection:"deny"` + provider/
quant pin is the guarantee. (Owner data-governance stance on file, 2026-07-02: research
prompts may route through OpenRouter provided no SSN/bank/PII data is ever in a prompt — none
is today.)

**Q5 — Judge pool + a NEW frontier-research subsystem.**
- (a) **3-family majority for RISKY-tier verdicts only.** SAFE/CANDIDATE stay 2 judges / 2
  families. RISKY verdicts require **2-of-3** agreement across 3 distinct families (Claude +
  Gemini + a third; an OpenRouter open-weight model may be the third only once
  judge-agreement-certified per §6, else another certified family). ≈+50% judge cost on the
  RISKY slice only. Wire via `JUDGE_POOL` (§6) + a per-tier judge-count.
- (b) **NEW Subsystem 1.5 — monthly pareto-frontier research + candidate rotation** (§10.1).
  This is the one substantive addition beyond the original design: the candidate pool is no
  longer a hand-maintained static `MODEL_LADDER`; a monthly cron researches the cross-provider
  frontier and the loop cycles through the most promising candidates over time.

**Q6 — `purpose=None`: HARD-DEPRECATE** (confirms §9-Q6). After the SayDo-fix trailing 30d
window clears, `call_llm(purpose=None)` raises `PurposeRequiredError` in dev/test and logs a
loud warning + routes to `DEFAULT_MODEL` in prod (fail-safe — never breaks a live call). The
anonymous-purpose alarm becomes a backstop for legacy paths only. Sequence as its own small
PR AFTER PR1, gated behind a check that residual `purpose=None` spend has fallen to near-zero.

### 10.1 Subsystem 1.5 — monthly pareto-frontier research + candidate rotation (Q5b)

Problem: `MODEL_LADDER` is a hand-edited constant; OpenRouter exposes hundreds of open-weight
models nobody curated; the sweep can't test every model×purpose weekly. The owner wants the
optimizer to (a) discover which models even belong in the candidate pool and (b) cycle through
the most promising over time.

- **`model_frontier_research`** — new operational LLM purpose (Opus + web/`WebSearch`, MONTHLY
  cron; operational recipe + the §1.5 hygiene touches: `LLM_MODELS` pin, `prompt_versions`,
  `META_PURPOSES`, `CAPTURE_DENYLIST`, ~$3/mo warn budget, `scope="meta_eval"`). Automates +
  extends the existing `model-frontier` reference / `/refresh-frontier` manual restamp:
  re-verify current cross-provider list prices (Claude · Gemini · OpenRouter), surface
  newly-released / newly-cheap models, and for OpenRouter filter its hundreds to a promising
  shortlist. Output (structured, validated): candidate rows `{model_id/slug, family, input/
  output $/MTok (verified), promise (cheap × plausibly-capable), source_url, rationale}`.
  Prices are indicative for ranking; the backend still records real charged cost per call.
- **`candidate_models` table** (new migration): the data-refreshed frontier the ladder
  consults for TESTING. Cols ≈ `(model_id PK, family, input_usd_per_mtok, output_usd_per_mtok,
  promise REAL, source TEXT('frontier_research'|'seed'|'manual'), status TEXT('active'|
  'retired'), first_seen_at, verified_at, notes, research_run_id)`. `MODEL_LADDER` stays the
  code seed + the type/price source of truth for seed rows; `candidate_models` OVERLAYS
  discovered rows. A merged accessor unions seed + `active` discovered rows so the inventory /
  `cheaper_candidates` / nominator see the live pool. **Discovered models AUTO-ENTER the TEST
  pool** (owner decision): eligible for `scope="model_eval"` replay immediately. PRODUCTION
  routing is unchanged and still gated by the full switch bar (Q3) — testing spends only
  eval $, never touches a user-facing call.
- **Coverage-tracked rotation:** the sweep cycles the highest-`leverage × promise` (purpose,
  candidate) pairs that are *due*, tracking last-tested-at per (purpose, candidate) (from
  `model_eval_verdicts.recorded_at`) so it fairly cycles the frontier over weeks instead of
  re-testing the same few. Extends the nominator ranking + `_HARVEST_STEPS`; a per-sweep cap
  bounds cost and whatever is deferred is `log()`ged (no silent truncation).
- **Cadence:** frontier research + nominator both run monthly in `run_weekly_model_eval`
  step 0, force-run when the candidate set changes materially (a new discovered model is
  exactly when re-nomination + re-prioritization pays).
- **Isolation:** `model_frontier_research` is meta machinery — `scope="meta_eval"`,
  `CAPTURE_DENYLIST`, excluded from the workload inventory (`EVAL_SCOPES`) and from the
  nominable universe. It reads *about* models; it never touches a production generation
  prompt (I2/I5).

### 10.2 Reconciliations discovered during build

- **`EVAL_SCOPES` must include `'eval'`.** §0's literal set `{model_eval, backend_judge,
  prompt_ab, meta_eval}` omits the rubric/golden harness scope `'eval'`, which
  `model_eval_panel._EVAL_SCOPES` already treats as measurement. The canonical `EVAL_SCOPES`
  constant (PR1, `src/llm/eval_scopes.py`) is the UNION
  `{model_eval, backend_judge, eval, prompt_ab, meta_eval}`, so a purpose's production cost is
  never inflated by its own grading traffic. PR6 unifies `model_eval_panel._EVAL_SCOPES` onto
  it.
- **Migration head** is past `0131` — pick number + `down_revision` at rebase time per the
  parallel-session-collision memory (verify at build time, not from this doc).

### 10.3 Revised build order

PR1 (inventory) is unchanged and remains the right first step. Deltas from §7:
- **PR3** absorbs Subsystem 1.5 → renamed "nominator + frontier research + rotation"; also
  builds the RISKY switch-audit record (Q3).
- **PR5** expands to build prompt auto-apply (`prompt_pin_overrides` + git-reconciliation
  trail), not human-PR-only (Q1).
- **PR6** adds the 3-family RISKY judging surface + the RISKY remediation/revert panel affordance
  (Q3/Q5a) and the frontier/rotation views.
- **New small PR (after PR1):** `purpose=None` hard-deprecation (Q6).

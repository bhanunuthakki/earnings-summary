# Plan: auto-seed KPI registry (inflection + thesis-breaker)

Status: PROPOSAL (plan only — no production code in this PR).
Author: planning session, 2026-05-30.
Scope owner on merge: the build chip / engineer who executes the build sequence in §10.

---

## 1. Summary

The KPI-inflection trigger ([`src/triggers/kpi_inflection.py`](../../src/triggers/kpi_inflection.py)) and the thesis-breaker escalation folded into it (PR #186) fire **nothing in production** because `user_kpi_registry` is empty: [`scan()`](../../src/triggers/kpi_inflection.py:466) returns `[]` the moment `_load_registered_kpis` finds no rows. Today the only way to populate the table is the manual N10 seeder ([`scratch/seed_kpi_registry.py`](../seed_kpi_registry.py)), whose `--propose`/edit-YAML/`--write` flow puts a human review gate in front of every row.

The fix is an **auto initial-seeding** mode that bootstraps the registry across the portfolio in one run, using the **Opus-tier** model to propose load-bearing KPIs, polarity, and thresholds — without per-row manual review.

This is a deliberate reversal of a human-in-the-loop design that was chosen *precisely because the registry decides which alerts fire*. The plan does not paper over that: it adopts a **confidence-gated** trust model (auto-write only the rows the machine is provably right about; route the rest to the existing YAML review), and it makes the two failure modes that a human gate used to catch — **wrong kpi_name** (silent no-fire) and **wrong threshold direction on a breaker** (fires on good news / silent on bad news) — *mechanically impossible to auto-write*, not merely discouraged by prompt text.

---

## 2. Approach — extend N10, don't rewrite

Add an **`--auto`** mode to `scratch/seed_kpi_registry.py`. The entire input-assembly layer of N10 is reused verbatim; the new surface is the Opus prompt/schema, a deterministic correctness layer, the confidence gate, and a direct-write path.

The two **load-bearing correctness pieces are pure functions with no LLM/CLI dependency**, so they belong in `src/` (importable, pyright-strict, unit-tested in `tests/`) rather than buried in a scratch script. The LLM call + CLI orchestration stays in `scratch/` per the one-shot-tool convention.

### Reuse map

| Reused verbatim from N10 (`scratch/seed_kpi_registry.py`) | Line(s) | Role in `--auto` |
|---|---|---|
| `_default_repo_root` / `_default_db_path` / `_default_fmp_dir` | 162–185 | path resolution (worktree-aware) |
| `_portfolio_tickers` (`list_type='portfolio'`) | 193–209 | the `--all` portfolio source (§7-orig task item) |
| `_recent_kpi_observations` (`SELECT kd.name AS kpi_name …`) | 212–272 | recent values for the prompt **and** source of exact names |
| `_format_kpi_observations_table` | 275–291 | prompt table |
| `_latest_10k_path` + `_extract_10k_narrative` + section helpers | 294–393 | Risk Factors / MD&A narrative |
| `load_thesis_anchor` (`src/llm/anchors.py:249`) | — | thesis anchor block |
| `render_yaml` / `_read_yaml_proposals` / `write_from_yaml` | 598–874 | the **review queue** for rows that fail the gate |
| `_call_llm_with_retry` retry/parse pattern | 527–568 | mirrored for the auto parser |

| New (this build) | Where |
|---|---|
| `Polarity` enum, `KNOWN_KPI_POLARITY` table, `infer_polarity_from_table`, `adverse_direction` | **NEW** `src/user_state/kpi_polarity.py` |
| `allowed_kpi_names(ticker, db_path, min_facts)` | **NEW** `src/user_state/kpi_catalog.py` |
| Register Opus purpose `kpi_registry_auto_proposal` | edit `src/llm/cli.py` `LLM_MODELS` (78–124) |
| `AutoProposal` dataclass, `_build_auto_proposal_prompt`, `_parse_auto_proposal_response`, gate, `auto_seed_ticker`, `--auto` CLI wiring | `scratch/seed_kpi_registry.py` (`--auto` mode) |

> Alternative considered: put `kpi_polarity` / `kpi_catalog` inside the scratch file and test via the `sys.path.insert(scratch)` trick the existing `tests/test_seed_kpi_registry.py:37` already uses. Acceptable, but rejected as the primary because these are reusable, pure, correctness-critical units that deserve first-class `src/` typing and tests. The trigger itself could later import `allowed_kpi_names` to validate registry rows at scan time.

---

## 3. Trust model decision — confidence-gated auto (RESOLVED)

**Decision: confidence-gated auto, not pure auto.**

Pure auto (every Opus proposal writes directly) is rejected: a single wrong-polarity breaker is catastrophic (fires on good news, silent on the actual deterioration — see §5), and a single hallucinated `kpi_name` writes a row that *looks* configured but can never fire (§4). The human gate existed to catch exactly these.

Confidence-gated auto keeps the speed win (bootstrap the whole portfolio in one run) for the cases the machine is provably right about, and **does not throw away the safety net** — it routes the genuinely ambiguous rows to the *existing* N10 YAML review flow. A proposal is **auto-written** (`scaffold_source='llm_auto'`) only when **all** hold:

1. **`name_validated`** — `kpi_name` is in `allowed_kpi_names(ticker)` (exact match, ≥ `_MIN_SERIES_LEN` facts). §4.
2. **`polarity_resolved`** — the known-KPI table agrees with the LLM, *or* the table has no entry and `confidence ≥ min_confidence`. A **table-vs-LLM polarity conflict is never auto-written**. §5.
3. **breaker grounding** — if `is_thesis_breaker`, then `threshold_value is not None` **and** `threshold_grounded` is true with a cited section. §7. (A breaker with a null threshold can never escalate — see §7 — so it must not be auto-written as a breaker.)
4. **`confidence ≥ min_confidence`** — default `0.75` on a 0–1 self-rating (tunable via `--min-confidence`).

Everything that fails any check is appended to a review YAML (status `proposed`, with the failure reason in `notes` and `threshold_direction` pre-derived so the human just flips `status: accepted`). The human then runs the unchanged `--write` path.

### `scaffold_source` taxonomy
- `'manual'` — hand-entered (e.g. `tests/test_trigger_kpi_inflection.py:206`).
- `'llm_proposal'` — human reviewed a YAML row and `--write` committed it ([`scratch/seed_kpi_registry.py:64`](../seed_kpi_registry.py:64); asserted at `tests/test_seed_kpi_registry.py:582`).
- `'llm_auto'` — **NEW**: written by `--auto` with no human review.

The migration docstring already documents `scaffold_source` as free-form (`'manual' | 'seeded_from_10k' | ...`, [`0060_user_kpi_registry.py:31,82`](../../alembic/versions/0060_user_kpi_registry.py:82)), so `'llm_auto'` needs **no schema change**.

### Interaction with the manual seeder (upsert semantics — RESOLVED)
Natural key is `(user_id, ticker, kpi_name)` ([`registry.py:61–67`](../../src/user_state/registry.py:61); unique index `uq_user_kpi_registry_user_ticker_kpi`, [`0060_…:87`](../../alembic/versions/0060_user_kpi_registry.py:87)). `upsert_kpi` overwrites **every** non-key column on conflict, including `scaffold_source` ([`registry.py:101–121`](../../src/user_state/registry.py:101)).

- **Human curates an auto row later**: user runs N10 `--propose`/`--write` for the same `(ticker, kpi_name)`. `--write` upserts with `scaffold_source='llm_proposal'` and the human's reviewed values → human review wins, and the source flips to record that it is now curated. ✅ No change needed; this falls out of the existing upsert.
- **`--auto` re-runs over an already-curated ticker**: a naive upsert would clobber the human's values. **Resolution: the auto path guards before writing** (see §8). It reads existing rows and **skips any row whose `scaffold_source` is not `'llm_auto'`** (preserves `manual` / `llm_proposal`). `registry.upsert_kpi` itself is left unchanged; the guard lives in the seeder.

---

## 4. Correctness #1 — kpi_name alignment (silent no-fire)

### The mechanism that makes a mismatch silent
`scan()` loads each registered KPI's series with `load_kpi_series(ticker=ticker, kpi_name=reg.kpi_name, …)` ([`kpi_inflection.py:473`](../../src/triggers/kpi_inflection.py:473)). That loader joins:

```sql
JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
WHERE kf.ticker = ? AND kd.name = ?          -- loaders.py:503–505 (and 536–539)
```

`kd.name = ?` is an **exact, case-sensitive** match (SQLite default `BINARY` collation). If the registry's `kpi_name` is not byte-identical to a `kpi_definitions.name` row for that ticker, the series is `[]`, `_candidate_from_series` bails at `len(series) < _MIN_SERIES_LEN` ([`kpi_inflection.py:498`](../../src/triggers/kpi_inflection.py:498)), and **no candidate, no alert, no error**. `"NIM"` ≠ `"Net Interest Margin"` ≠ `"nim"`. N10 only *asks* the LLM to reuse names ("Prefer names already present… verbatim", [`seed_kpi_registry.py:431,438`](../seed_kpi_registry.py:438)); the human gate is what actually caught drift. Auto seeding has no human gate, so this must be enforced in code.

### Enforcement (three layers — closed set, hard validation, fail-closed)

1. **Closed-set prompt.** `_build_auto_proposal_prompt` enumerates the **exact** candidate names and instructs the model to choose `kpi_name` *only* from that list, copied verbatim. The candidate list is `allowed_kpi_names(ticker)`.

2. **`allowed_kpi_names(ticker, db_path, min_facts=_MIN_SERIES_LEN)`** — NEW pure helper in `src/user_state/kpi_catalog.py`. Returns the set of `kpi_definitions.name` strings for the ticker that have **≥ 8 quarterly `kpi_facts` rows** (`fiscal_period_type IN ('Q1','Q2','Q3','Q4')`). The `≥ 8` floor mirrors `_MIN_SERIES_LEN` ([`kpi_inflection.py:81`](../../src/triggers/kpi_inflection.py:81)) so every name in the set can actually produce a fireable series — name-validity and fire-ability are the *same* gate. (Names that exist but have < 8 facts are excluded: they would pass a naive "exists?" check yet never fire.)

3. **Hard post-parse validation (fail-closed).** Any proposal whose `kpi_name ∉ allowed_kpi_names` is **dropped from the auto path** and logged (`event: auto_seed_name_rejected`). It is *not* written and *not* even sent to review (a name the pipeline doesn't track is not actionable by the human either) — but it is surfaced in the run summary so over-restriction is visible.

### Deliberate scope limit (state it, don't hide it)
Auto seeding covers only the intersection **{thesis-relevant} ∩ {≥ 8 quarterly facts in `kpi_facts`}**. Qualitative breakers and KPIs the pipeline doesn't numerically extract (e.g. a regulatory-ruling breaker) **cannot** be auto-seeded as fireable rows — they remain manual-seeder territory. This is a feature: it guarantees every auto-written row is wired to data the trigger can evaluate. The run summary logs the count of thesis-relevant KPIs skipped for lack of facts so the gap is explicit (no silent truncation).

---

## 5. Correctness #2 — adverse-direction breaker thresholds (the #1 risk)

### Why direction is load-bearing
Escalation requires a real adverse cross: `_is_thesis_breaker_cross` returns true only when `is_thesis_breaker is True AND threshold_crossed is True` ([`kpi_inflection.py:288–291`](../../src/triggers/kpi_inflection.py:288)). `threshold_crossed` comes from `_crosses_threshold(direction, threshold, curr_value)` ([`kpi_inflection.py:527`](../../src/triggers/kpi_inflection.py:527)):

```python
if direction == "below": return value < threshold     # kpi_inflection.py:223
if direction == "above": return value > threshold      # kpi_inflection.py:225
```

The trigger **assumes** the breaker threshold is registered in the **adverse** direction — its own docstring says so and pins responsibility on the seeder ([`kpi_inflection.py:283–287`](../../src/triggers/kpi_inflection.py:283); the `sizing_update` escalation at [`kpi_inflection.py:721–737`](../../src/triggers/kpi_inflection.py:721) repeats the assumption). Get it wrong and:

- **Higher-is-better KPI (e.g. NIM, NDR) registered `above`**: fires when the metric goes **up** (good news → false "thesis-breaker crossed" + sizing review), and is **silent when it falls** (the real break). Catastrophic.
- **Lower-is-better KPI (e.g. churn, NPL) registered `below`**: fires when churn **drops** (good), silent when it **spikes** (bad). Catastrophic.

### The mechanism — derive direction from polarity; never let the LLM set it directly
The most robust judgment to ask a model for is *"is higher or lower better for this KPI?"* — not *"which threshold_direction should fire?"* (which conflates the metric's polarity with the comparator and is where models slip). So:

1. The LLM outputs **`polarity ∈ {higher_is_better, lower_is_better}`** and a `threshold_value`. It **does not** output `threshold_direction`.
2. Code derives the comparator: **`threshold_direction = adverse_direction(resolved_polarity)`**, where
   ```
   adverse_direction(higher_is_better) -> "below"
   adverse_direction(lower_is_better)  -> "above"
   ```
   This applies to **all** auto-written thresholds (breakers and thresholded non-breakers) so the registry uniformly encodes "alert me when this load-bearing KPI deteriorates." (A human can later flip a non-breaker to celebrate-on-good-news; auto seeding defaults to adverse.)
3. `resolved_polarity` **prefers the deterministic table** when it has an entry; falls back to the LLM's polarity otherwise.

### Polarity table + LLM, cross-checked (RESOLVED: both)
`src/user_state/kpi_polarity.py` (NEW):

```python
class Polarity(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER  = "lower_is_better"

# Substring-keyed, lowercased match against kpi_name. Authoritative for known KPIs.
KNOWN_KPI_POLARITY: dict[str, Polarity] = {
    "net interest margin": HIGHER_IS_BETTER,  "nim": HIGHER_IS_BETTER,
    "net dollar retention": HIGHER_IS_BETTER, "ndr": HIGHER_IS_BETTER,
    "net revenue retention": HIGHER_IS_BETTER,
    "gross margin": HIGHER_IS_BETTER, "operating margin": HIGHER_IS_BETTER,
    "free cash flow": HIGHER_IS_BETTER, "fcf margin": HIGHER_IS_BETTER,
    "gmv": HIGHER_IS_BETTER, "arr": HIGHER_IS_BETTER, "take rate": HIGHER_IS_BETTER,
    "roe": HIGHER_IS_BETTER, "return on equity": HIGHER_IS_BETTER,
    # lower-is-better
    "churn": LOWER_IS_BETTER, "npl": LOWER_IS_BETTER,
    "non-performing": LOWER_IS_BETTER, "cost of risk": LOWER_IS_BETTER,
    "cac payback": LOWER_IS_BETTER, "leverage": LOWER_IS_BETTER,
    "net charge-off": LOWER_IS_BETTER, "default rate": LOWER_IS_BETTER,
}
```

`infer_polarity_from_table(kpi_name)` lowercases and returns the matching `Polarity` or `None`. Resolution + gate:

- **table hit, LLM agrees** → `polarity_resolved` ✅, use the table polarity.
- **table hit, LLM disagrees** → `polarity_conflict`: **never auto-write**, route to review (the disagreement is itself the signal that direction is uncertain).
- **no table entry** → use the LLM polarity but require `confidence ≥ min_confidence`; otherwise route to review.

### Worked examples (must appear as assertions in the test suite)
| KPI | polarity | adverse `threshold_direction` | breaker fires when |
|---|---|---|---|
| NIM (bank) | higher_is_better | **below** | NIM `< floor` (compression) |
| Net dollar retention | higher_is_better | **below** | NDR `< floor` (e.g. < 110%) |
| FCF margin | higher_is_better | **below** | FCF margin `< floor` |
| Monthly churn | lower_is_better | **above** | churn `> ceiling` |
| NPL ratio (bank) | lower_is_better | **above** | NPL `> ceiling` |
| CAC payback (months) | lower_is_better | **above** | payback `> ceiling` |

This reproduces exactly the production fixture: NU NIM `below 17` as a breaker ([`tests/test_trigger_kpi_inflection.py:736–744`](../../tests/test_trigger_kpi_inflection.py:736)).

---

## 6. LLM module — Opus proposal

### Model identifier (RESOLVED)
The repo's established Opus id is **`claude-opus-4-7`** — used for every Opus purpose today (`company_description`, `valuation_basis`, `saydo_importance` in [`src/llm/cli.py:99,115,118`](../../src/llm/cli.py:99), plus reverse-DCF / exec-comp / cross-portfolio lenses). Use `claude-opus-4-7` for consistency. (A newer `claude-opus-4-8` exists; switching is a one-line follow-up if/when the repo standardizes — out of scope here.)

**Wiring (project-convention way):** register a **new purpose** in `LLM_MODELS` ([`cli.py:78`](../../src/llm/cli.py:78)):
```python
"kpi_registry_auto_proposal": "claude-opus-4-7",
```
The `--auto` path calls `call_llm(prompt, purpose="kpi_registry_auto_proposal", ticker=ticker)` with **no `model=` override**, so `_model_for` ([`cli.py:127`](../../src/llm/cli.py:127)) resolves Opus. Benefits over passing `model=` ad hoc: single canonical place for the model choice, and a distinct budget-attribution key so portfolio-wide auto-seeding spend is separable. The **manual N10 purpose `kpi_registry_proposal` stays unregistered → Sonnet** (its deliberate choice, [`seed_kpi_registry.py:53–57`](../seed_kpi_registry.py:53)) — the two modes diverge cleanly.

### Output schema (one JSON object per proposal)
```jsonc
{
  "kpi_name":          "string — VERBATIM from the supplied candidate list",
  "polarity":          "higher_is_better | lower_is_better",
  "is_thesis_breaker": true,
  "threshold_value":   17.0,            // number | null
  "threshold_grounded": true,           // true ONLY if threshold cited to source
  "grounding_citation": "Item 1A Risk Factors — '…funding-cost pressure…'",
  "confidence":        0.0,             // 0..1 self-rating across (name+polarity+threshold)
  "rationale":         "2-3 sentences."
}
```
Note the absence of `threshold_direction` — derived in code (§5).

### Prompt design (key clauses)
- Inputs reuse N10 assembly: thesis anchor (`load_thesis_anchor`), recent KPI table (`_recent_kpi_observations`), 10-K Risk Factors / MD&A (`_extract_10k_narrative`).
- **Closed name set**: "Choose `kpi_name` ONLY from this list, copied character-for-character: [<allowed names>]. Do not invent or rephrase. If none fit a thesis-critical metric, return fewer proposals."
- **Polarity, not direction**: "Report `polarity` = whether HIGHER or LOWER is good for the thesis. Do not specify any alert direction; the system derives it."
- **Grounding**: "Set `threshold_value` non-null and `threshold_grounded:true` ONLY when a specific level is stated in the 10-K or thesis anchor; cite the section in `grounding_citation`. Otherwise `threshold_value:null`, `threshold_grounded:false`."
- **Breaker discipline**: "Set `is_thesis_breaker:true` ONLY when source text names a level past which the business model breaks **and** you can ground a `threshold_value`."
- **Calibrated confidence**: "`confidence` reflects joint certainty in name+polarity+threshold; reserve > 0.75 for grounded, unambiguous cases."

### Retry / parse
Mirror `_call_llm_with_retry` ([`seed_kpi_registry.py:527`](../seed_kpi_registry.py:527)): one retry with the `_JSON_RETRY_PREAMBLE` on parse failure, then `SeederLLMError`. `_parse_auto_proposal_response` validates types and **rejects** unknown `polarity` strings and bool-as-number `threshold_value` (reuse the bool-guard pattern at [`seed_kpi_registry.py:474`](../seed_kpi_registry.py:474)). No permissive fallbacks — a structurally bad object fails the parse, consistent with project conventions.

---

## 7. Threshold-grounding policy (RESOLVED)

Behavior of a registry row under the trigger:
- **Threshold set** → `should_fire` requires a directional cross ([`kpi_inflection.py:592–593`](../../src/triggers/kpi_inflection.py:592)).
- **Threshold null** → falls back to the `|zscore| ≥ _SIGNIFICANT_ZSCORE (2.0)` path ([`kpi_inflection.py:595–597`](../../src/triggers/kpi_inflection.py:595)) — fires on any sharp inflection, either direction.

Resolution by row type:

- **Breaker rows MUST be grounded.** A breaker with a null threshold can **never** escalate: `_is_thesis_breaker_cross` needs `threshold_crossed=True`, which needs a non-null threshold. So an ungrounded breaker would sit in the registry as a "breaker that can't break" and only ever emit a plain z-score inflection. **Auto-write a breaker ONLY when `threshold_grounded` and `threshold_value is not None`; otherwise route to review** (do not silently demote to non-breaker — that hides the analyst's intent; let the human supply the level).

- **Non-breaker rows may be null-threshold.** This is the framework's *designed* z-score path: "track this KPI, alert on sharp moves." Acceptable to auto-write. It is the main contributor to noise, so it is bounded by `--max-auto-per-ticker` (default e.g. 6) and the confidence gate, and the run summary reports how many null-threshold rows were written.

Net: auto-written **breakers are always precise** (grounded adverse cross); auto-written **non-breakers** give useful inflection coverage and degrade to z-score when ungrounded.

---

## 8. Idempotency & re-run (RESOLVED)

`--auto` is **re-runnable**, not one-shot. Per proposed row, before writing, read existing rows via `list_kpis(user_id, ticker, db_path)` and branch on the matching natural key:

| Existing row | Action |
|---|---|
| none | **write** `llm_auto` |
| `scaffold_source == 'llm_auto'` | **overwrite** (refresh) — unless `--only-new` is set |
| `scaffold_source` in {`manual`,`llm_proposal`,other} | **skip**, log `event: auto_seed_preserved_curated` |

This guarantees re-runs never clobber human curation, and refreshing machine-written rows is safe because they were machine-written anyway. `--only-new` makes re-runs purely additive (skip any existing key) for users who want auto-seeding to never touch prior auto rows. Because Opus is non-deterministic, an unguarded refresh could flip an `llm_auto` row's values between runs; `--only-new` (or simply not re-running) avoids that, and it is called out in the tool's `--help`. `registry.upsert_kpi` is **not** modified — the guard is entirely in the seeder.

`--dry-run` logs the full write/skip/review plan and the derived `threshold_direction` for every row without calling `upsert_kpi` (mirror N10's dry-run, [`seed_kpi_registry.py:825–836`](../seed_kpi_registry.py:825)).

---

## 9. Test strategy

All LLM calls mocked via `monkeypatch.setattr("seed_kpi_registry.call_llm", _StatefulLLM([...]))` (existing pattern, [`tests/test_seed_kpi_registry.py:243`](../../tests/test_seed_kpi_registry.py:243)). DB fixtures reuse the alembic-stamp-then-add-inputs-schema pattern ([`tests/test_seed_kpi_registry.py:59–128`](../../tests/test_seed_kpi_registry.py:59)).

**Pure-unit tests (PR-A, no LLM/DB):** `tests/test_kpi_polarity.py`
- `adverse_direction(HIGHER_IS_BETTER) == "below"`; `adverse_direction(LOWER_IS_BETTER) == "above"`.
- table lookups: NIM/NDR/FCF → higher; churn/NPL/CAC-payback → lower; case-insensitive substring; unknown → `None`.

**Name catalog (PR-A):** `tests/test_kpi_catalog.py`
- name with ≥ 8 quarterly facts → in set; with 4 facts → excluded; FY-only facts → excluded; ticker isolation; exact-string (case-sensitive) membership.

**Auto-mode tests (PR-C):** `tests/test_seed_kpi_registry_auto.py`
1. **name-alignment** — LLM proposes `"Net Interest Margin"` when the catalog only has `"NIM"` → dropped from auto path, **no registry row written**, logged rejected. A proposal of `"NIM"` (in catalog) → written.
2. **adverse-direction breaker** — LLM returns `polarity=higher_is_better, is_thesis_breaker=true, threshold_value=17, grounded=true` for `"NIM"` → the written row has `threshold_direction == "below"` and `is_thesis_breaker is True`. Symmetric case: `polarity=lower_is_better` churn breaker → `threshold_direction == "above"`.
3. **polarity-conflict → review** — table says higher, LLM says lower → not auto-written; appears in the review YAML with the conflict noted.
4. **confidence gate** — `confidence` below `min_confidence` and no table entry → routed to review, not auto-written.
5. **grounded-breaker required** — breaker with `threshold_value=null` / `grounded=false` → routed to review, **not** written as a breaker.
6. **idempotent re-run** — run twice: an `llm_auto` row is overwritten (same `id`, no duplicate); a pre-seeded `manual`/`llm_proposal` row on the same key is **preserved** (values unchanged, source unchanged).
7. **end-to-end fires** — seed `kpi_facts` for NU `"NIM"` with `_VALUES_RECENT_DOWN` ([`tests/test_trigger_kpi_inflection.py:48`](../../tests/test_trigger_kpi_inflection.py:48), inflects to 16.4), run `--auto` (mock Opus → grounded NIM breaker), then drive `KpiInflectionTrigger().scan/should_fire/build_alert/draft_actions` exactly like [`test_full_pipeline_integration_smoke`](../../tests/test_trigger_kpi_inflection.py:730). Assert: one candidate, `threshold_crossed is True`, escalated memo contains `"THESIS-BREAKER CROSSED"`, actions == `["earnings_prep_append","sizing_update","thesis_update"]`. **This is the proof the auto-seeder actually makes the trigger fire** — it closes the loop on both correctness risks at once.

**Regression:** existing `tests/test_seed_kpi_registry.py` must stay green (the `--propose`/`--write` paths are untouched).

---

## 10. Risks & open questions

| Risk | Severity | Mitigation in this plan |
|---|---|---|
| Wrong-polarity breaker (fires on good news / silent on bad) | **Catastrophic** | LLM never sets direction; code derives from polarity; deterministic table is authoritative; table-vs-LLM conflict → review (§5). E2E test asserts NIM→`below`. |
| `kpi_name` mismatch → silent no-fire | **High (silent)** | Closed-set prompt + `allowed_kpi_names` (≥ 8 facts) + fail-closed drop (§4). Name-alignment test. |
| Ungrounded breaker that can never escalate | Medium | Grounded-threshold required to auto-write a breaker; else review (§7). |
| Opus cost across the portfolio | Medium | One Opus call per ticker, run once; distinct budget purpose `kpi_registry_auto_proposal` for attribution/caps; `--ticker` for incremental. Bounded like the other per-ticker Opus purposes. |
| Over-seeding noise (many null-threshold z-score rows) | Medium | `--max-auto-per-ticker` cap + confidence gate; non-breaker null-threshold counts reported in the run summary. |
| Clobbering human curation on re-run | Medium | `scaffold_source` skip-guard (§8); `--only-new`. |
| Opus non-determinism flips `llm_auto` values across runs | Low | `--only-new` for additive re-runs; documented in `--help`. |

**Open questions for the build owner (defaults chosen so the build is unblocked):**
1. `min_confidence` default — proposed **0.75**. Tune after a first portfolio dry-run.
2. `--max-auto-per-ticker` default — proposed **6** (N10 default is 5 proposals, [`seed_kpi_registry.py:59`](../seed_kpi_registry.py:59)).
3. Should auto-written **non-breaker** thresholds also be adverse-only, or omit thresholds entirely and rely on z-score? Plan chooses **adverse-only when grounded, else null/z-score** — uniform and least surprising; revisit if z-score noise dominates.
4. `claude-opus-4-7` vs `claude-opus-4-8` — plan picks `-4-7` for repo consistency; trivial to bump.

---

## 11. Build sequence (ordered; each independently shippable)

- **PR-A — correctness core (`src/`, pure, no LLM/DB-write):** add `src/user_state/kpi_polarity.py` (`Polarity`, `KNOWN_KPI_POLARITY`, `infer_polarity_from_table`, `adverse_direction`) and `src/user_state/kpi_catalog.py` (`allowed_kpi_names`). Ship with `tests/test_kpi_polarity.py` + `tests/test_kpi_catalog.py`. Zero API cost, highest-value safety net, lands the two correctness mechanisms before any wiring.
- **PR-B — model wiring (`src/llm/cli.py`):** register `"kpi_registry_auto_proposal": "claude-opus-4-7"` in `LLM_MODELS`; add a budget row if `src/llm_budget.py` requires per-purpose registration. Tiny; verifiable via `_model_for`.
- **PR-C — `--auto` mode (`scratch/seed_kpi_registry.py`):** `AutoProposal`, `_build_auto_proposal_prompt` (closed name set + polarity schema), `_parse_auto_proposal_response` (+retry), the gate (imports PR-A), `auto_seed_ticker` (write `llm_auto` with the §8 guard; emit residual review YAML via the existing `render_yaml`), CLI flags `--auto` / `--ticker` / `--all` / `--min-confidence` / `--max-auto-per-ticker` / `--only-new` / `--review-out` / `--dry-run`. Ship with `tests/test_seed_kpi_registry_auto.py` (all of §9). Depends on PR-A + PR-B.
- **PR-D (optional, can fold into PR-C):** a `--report` summary (per ticker: auto-written / routed-to-review / dropped-name / skipped-no-facts counts) and a short runbook note in the seeder docstring.

End of plan.

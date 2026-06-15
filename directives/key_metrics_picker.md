# Directive: "key metrics" preselect bubble row for the DIY metric picker

**Status: SHIPPED 2026-06-15.** One feature on top of the Research → Explore
"DIY" metric picker (`src/pipeline/explore_panel.py`): a row of clickable
preselect bubbles of the metrics MOST important to the selected ticker(s).
Clicking a bubble toggles that metric's `<option>` in the picker's existing
`<select multiple>` set. Built concurrently with — and deliberately disjoint
from — the S5 picker widen (search / cap-lift / cross-ticker de-fragmentation),
which owns `viewspec.engine.metric_catalog`.

## Why

The DIY picker exposes every extracted fact as a flat `<select multiple>`. That
is exhaustive but undiscoverable: a digital bank's load-bearing figures (NIM,
NPL, deposits, efficiency ratio) sit in an alphabetical list of hundreds of
captured numbers with no signal of importance. The bubble row puts the handful
that matter one tap away.

## Two sources, merged at render

1. **Tier-graded baseline — deterministic, always available, NO LLM.** The
   tier-1/tier-2 KPI grading already in `kpi_definitions.threshold_tier`
   (`tier_1_break` / `tier_2_monitor` — the `ThesisTier` enum; there is no
   tier-3). For the selected tickers, every graded definition with ≥1 fact maps
   `name → kpi:<name>`. Rendered straight from the DB on every render.
   `src/pipeline/key_metrics.py::tier_graded_baseline`. Reads only
   `threshold_tier` (a long-standing column) so it is safe on a DB not yet
   migrated to the capture-program `definition_origin` head (0113).

2. **LLM augmentation — runs on `--enable-llm`, NEVER on the render path.**
   `src/compute/key_metrics.py` ranks the metrics most important to the business
   over the FULL picker vocabulary (so it can surface important CAPTURED metrics
   — `definition_origin = 'capture'` — the tier grading hasn't reached yet),
   business-model aware. Mirrors `src/compute/peer_selection.py` exactly:
   - purpose `key_metrics` in `llm.cli.LLM_MODELS` (Sonnet to start; eligible for
     the eval-gated cheaper-at-parity downgrade per `cheapest_model_routing.md`);
   - schema-validated structured output via `llm.structured.call_llm_structured`
     (`KeyMetricSuggestion{token, why}`); every returned token must be one of the
     catalog tokens handed to the model (closed-vocabulary pick) or it is dropped;
   - cached to `data/key_metrics/{TICKER}.json` keyed on an input sha256 (name +
     business description + vocabulary), re-run only on `--refresh` / input change;
   - degrades to the tier-graded baseline on ANY LLM/parse/budget failure — the
     skip reason is recorded in the cache, the build never aborts.

## Render path (no heavy import, no LLM)

`src/pipeline/key_metrics.py::key_metric_bubbles` reads the cache JSON directly,
merges it onto the tier-graded baseline (tier first, then LLM picks not already
present), dedupes by token, validates every token against the live catalog (so a
click always maps to a real `<option>`), and caps the row. Absent cache → tier
baseline only. `explore_panel.py` renders the row above the pickers; the inline
JS routes a clicked token to the right `<select>` by its domain prefix
(`fin:` / `kpi:` / `seg:`) and re-fetches the row (`?fragment=keymetrics`) when
the ticker set changes.

## LLM governance (project rule)

- **Model-picker:** `LLM_MODELS["key_metrics"]` (Sonnet).
- **Structured output:** `KeyMetricSuggestion` Pydantic model, validated +
  vocabulary-checked at the call boundary.
- **Eval:** mode-A recall golden set `evals/golden/key_metrics.json` graded by
  `src/evals/key_metrics.py` (did the generator surface the must-have key metrics
  for the business model?), wired into `execution/run_llm_evals.py --purpose
  key_metrics` + `--coverage`.
- **Cost/latency/failure logging:** via the standard `call_llm` ledger. One
  cached call per ticker on the LLM build — cost bounded.

## Out of scope (S5 owns these)

- Search, the picker cap-lift (`limit_per_domain`), and cross-ticker metric
  de-fragmentation / normalization. This feature does NOT touch
  `viewspec.engine.metric_catalog`; it only reads it.

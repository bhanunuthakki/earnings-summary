# Explore ↔ Live DCF linking proposal

**Status:** prototype architecture note. It does not authorize schema, API, workbook, or production-route changes.

## Outcome

An analyst can move from an Explore result into the current DCF only when the visible metric has an exact semantic binding to a live model line item. The analyst can adjust forecast assumptions inline, preview the deterministic valuation impact, and explicitly apply a reviewed change. Reported observations remain immutable.

Metrics with no exact live-model binding show no **Live DCF** affordance. A separate **Propose model logic** workflow handles structural additions.

## What exists today

- `src/pipeline/explore_panel.py` exposes **Inject as DCF driver** for one selected metric and one target from the fixed `DRIVER_FIELDS` registry.
- `POST /api/dcf/inject-fact` resolves the latest governed fact, converts and bounds its unit, applies it to `RedesignInputs`, calls `refresh_dcf.apply_edits`, and records fact lineage in the assumptions JSON.
- `src/report/renderers/workspace_dcf.py` has a second KPI → DCF path. It recognizes a small set of KPI names, fills the in-page editor, and waits for the ordinary save action.
- `POST /api/dcf/inject-fact-sheet` parks an observed value in a separate reference workbook without changing the valuation.

These paths are useful but narrow. They map a fact to a scalar driver, cannot express a new model node or formula, and do not establish an exact semantic relationship between an Explore metric and a model line item. The generic segment-growth driver also changes every segment uniformly.

## UX contract

1. Explore renders a **Live DCF** doorway only when the binding resolver returns `current` for the current ticker, canonical metric identity, definition revision, scope, unit, and live DCF revision.
2. Selecting it opens an inline projection editor. Historical observations are read-only; only forecast inputs or formula parameters are editable.
3. Every edit creates an in-memory or durable draft against an exact base model revision. Deterministic recompute shows the fair-value and forecast delta.
4. **Review DCF change** shows the typed diff, source analysis, base revision, and expected new revision.
5. **Apply to live DCF** is the sole mutating action. A revision conflict returns to review; it never last-write-wins.
6. A metric without a binding has no DCF badge. **Propose model logic** is a separate, deliberate workflow—not a weaker form of injection.

## Proposed contracts

### Metric coordinate

The Explore result must carry the canonical coordinate already required for decision-grade facts:

- issuer identity;
- canonical metric identity and effective definition revision;
- scope and segment identity;
- fiscal period and cadence;
- unit, currency, accounting basis, and observation version;
- source manifest.

Display labels never participate in binding resolution.

### DCF metric binding

A typed binding associates a model revision node with a metric coordinate:

```text
binding_id
ticker
model_revision_id
model_node_id
canonical_metric_id
metric_definition_revision_id
scope_identity
role: actual_anchor | forecast_driver | reference
transform: direct | growth | ratio | rolling_average | sum
unit_contract
refresh_policy
status: current | stale | definition_conflict | orphaned
```

The binding is versioned with the model. A definition break never silently carries forward.

### DCF change draft

```text
draft_id
ticker
base_model_revision_id
base_input_sha256
source_analysis_id
source_observation_manifest_sha256
typed_patch
preview_run_id
status: editing | ready | applied | conflicted | discarded
logical_idempotency_key
```

The typed patch addresses a model node and parameter. It never names a workbook cell.

### Model revision

The live model should become an immutable revisioned model specification. `dcf_runs` remains the immutable calculated output. The workbook and assumptions JSON become controlled projections/import adapters during migration, not peer authorities.

For the first slice, the existing `RedesignInputs` payload plus its `input_sha256` can act as the revision body. `refresh_dcf.apply_edits` remains the one promotion path. A later structural-model slice adds a typed graph:

- source-metric node;
- assumption-series node;
- computed node using a closed formula vocabulary;
- aggregation node;
- valuation-output node.

No arbitrary spreadsheet formula or free-form Python enters through the UI.

## Directional synchronization law

This should not be implemented as symmetric cell synchronization.

```text
Governed observations ──read-only binding──▶ DCF actual anchors
         │                                      │
         └──────────────▶ Explore ◀──────── projected series
                              │
                         change draft
                              │ deterministic preview
                              ▼
                    review + compare-and-swap
                              │
                              ▼
                       new DCF revision
```

- Facts → DCF: admitted observations may update historical anchors under an explicit refresh policy. They never overwrite owner forecast assumptions.
- DCF → Explore: the current model revision publishes projected series as a derived read model with revision identity and calculation lineage.
- Explore → DCF: Explore emits a proposal against a base revision; it does not directly edit facts, workbook cells, or the live revision.
- Workbook → model: owner workbook edits are imported as a revision diff against the last exported revision. Drift produces a reconciliation task, not last-write-wins.
- Definition change: the binding becomes `definition_conflict`; the DCF doorway becomes a review warning until remapped.

## Adding new projection logic

**Propose model logic** builds a candidate typed subgraph:

1. choose the output line item;
2. bind governed historical metrics;
3. select a closed projection method or compose allowed nodes;
4. define forecast parameters and aggregation behavior;
5. validate units, periods, dependency cycles, segment reconciliation, definition continuity, and coverage;
6. recompute the whole DCF and show a structural diff;
7. approve a new model revision or discard the proposal.

For segment revenue, the normal shape is one source binding per reported segment, one base-period Observation Version, and segment-specific growth curves. The current “set all segments to one rate” injection must not power this workflow.

## Smallest coherent implementation

1. Add an explicit binding resolver for existing `RedesignInputs` fields and per-segment growth paths.
2. Return binding metadata alongside Explore result rows.
3. Add a draft/preview API that recomputes without persistence.
4. Add compare-and-swap apply using the current `input_sha256` and `refresh_dcf.apply_edits`.
5. Publish current DCF projections back into Explore as a derived series.
6. Retire the old direct Explore injection UI after parity; keep **Add as reference** as a distinct non-model action.

Structural line-item and formula creation should follow as a second slice after the revision and binding contracts prove stable.

## Acceptance evidence

- A bound metric renders **Live DCF**; an unbound metric does not.
- Definition, unit, scope, or revision mismatches fail closed.
- Preview changes no durable state.
- Apply produces one new model revision and one new `dcf_runs` version with complete source-analysis and observation lineage.
- Applying against a superseded base revision returns a conflict and preserves both versions.
- New actuals may mark a model stale but do not overwrite an owner forecast.
- A prior revision can be restored through a new compensating revision.

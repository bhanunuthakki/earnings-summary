# Per-ticker enhancements

**Status**: Layer 1 baseline. Added in Phase 5.

## Goal

Some tickers warrant research the universal brief pipeline doesn't cover —
patent expiry timelines for NVO, drug pipeline milestones for biotech names,
regulatory readout calendars, alt-data ingestion (e.g. IQVIA Rx volumes).
These are narrow per-ticker concerns that don't justify a new generic
section in the brief, but they should still surface in the brief's analysis
when relevant.

The Phase 5 convention is a thin data-flow pattern:

  1. A per-ticker script writes a JSON file to a canonical location.
  2. The brief's section builders look there and inline the file into LLM
     prompt context where it grounds analysis.
  3. The result: failure modes / bear-case observations / qualitative
     breakers automatically reference the per-ticker research when the
     LLM section runs.

No plugin registry, no new section types, no ReportSpec changes.

## Canonical location

```
data/ticker_specific/<TICKER>/<feature>.json
```

- `<TICKER>` matches `tracked_companies.ticker` exactly (uppercase).
- `<feature>` is a short, descriptive slug — `patent_timeline`,
  `pipeline_milestones`, `regulatory_calendar`, etc.
- The file is JSON. Any valid shape — the section builder reads it as
  `{path.stem}` and inlines the raw text in the prompt's TICKER-SPECIFIC
  CONTEXT block, so the LLM sees a labelled code-fence per feature.

## Who reads these files

- **`src/report/sections/bear_case.py`** — when generating the §9 Bear case
  failure modes, the section's `_ticker_specific_md` helper concatenates
  every JSON in `data/ticker_specific/<TICKER>/` and threads them into the
  LLM prompt. The model can then cite "Semaglutide US patent expires 2031
  (per `patent_timeline`)" inside a failure-mode hypothesis.
- Future section builders can call the same helper pattern when their
  analysis would benefit from per-ticker grounding.

The brief's structural shape (the 11 sections, the trigger ladder, the
provenance audit) is unchanged. Per-ticker enhancements only affect
content the LLM generates within the existing sections.

## Who writes these files

Whatever extractor produces the data, scheduled at whatever cadence makes
sense for the data source.

Current writers:

- **`execution/extract_nvo_patent_timeline.py`** — runs after each NVO
  annual report (cadence: yearly). Writes the canonical
  `data/ticker_specific/NVO/patent_timeline.json` plus a dated audit
  snapshot at `.tmp/nvo_patents/nvo_self_disclosed_<DATE>.json`.

To add a new per-ticker enhancement:

  1. Write an extractor script under `execution/`.
  2. Have it write to `data/ticker_specific/<TICKER>/<feature>.json`.
  3. Schedule it on whatever cadence the data source supports (annual,
     quarterly, weekly).
  4. That's the whole integration — the brief picks it up on next render.

## What this is NOT

- **Not a generic plugin system.** No registration, no introspection.
  Files in the canonical directory get loaded; that's it.
- **Not a place for universal data.** Anything that applies to every
  ticker (financials, segments, KPIs) belongs in the canonical pipeline
  (`financial_facts`, `kpi_facts`, etc.). The ticker-specific directory is
  for ticker-uniquely-relevant detail.
- **Not a section-bypass mechanism.** Per-ticker JSONs feed prompt context;
  they don't create new sections or override the §1–§11 structure.

## Existing per-ticker enhancements

| Ticker | Feature | Writer | Output |
|---|---|---|---|
| NVO | Patent expiry timeline | `execution/extract_nvo_patent_timeline.py` | `data/ticker_specific/NVO/patent_timeline.json` |

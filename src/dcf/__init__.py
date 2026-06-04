"""DCF subsystem (redesigned model).

The canonical DCF for each ticker is the redesigned 9-sheet workbook at
`dcf/<TICKER>.xlsx` — formula-driven and Google-Sheets-native (Cover · Dashboard ·
Color Code · WACC · Model · Financials · Consensus · Valuation · Monte Carlo).

Lifecycle:
  - `execution/build_redesigned_dcf.py` builds the workbook from FMP data and the
    per-name Opus assumption pass (`data/dcf_assumptions/<T>.json`).
  - `execution/refresh_dcf.py` (`refresh_one`) rebuilds it from the latest FMP,
    preserves the user-owned Dashboard inputs (the yellow assumption cells), and
    recomputes the value-of-record into `dcf_runs` (which briefs read from).

Modules:
  redesign   — read a workbook's inputs + recompute fair value; capture/inject
               Dashboard inputs for edit-preservation (the live reader/engine)
  valuation  — the Damodaran engine: FCFF → operating value → equity bridge →
               value/share, with an EV exit multiple + implied price multiples
  live_price — current market price from the multi-source stack (`sources.price`)
  persist    — upsert the `dcf_runs` row briefs read from
"""

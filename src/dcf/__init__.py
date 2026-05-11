"""DCF subsystem.

Phase 3 implementation. The canonical DCF workbook lives at
`dcf/<TICKER>.xlsx` — user-maintained, hybrid round-trip:
  - User edits forecast assumptions, model formulas, segment cells in Excel.
  - System reads the FCF stream and diluted-shares cells from the Valuation
    sheet and re-computes PV/share at the per-ticker WACC (from
    `holdings/<TICKER>.json`).
  - PV / live-price comparison drives the trim/sell triggers per the design
    (>10% over → trim, >20% over → sell).

For Phase 3 the system is read-only against the workbook. Seeder + refresher
(automatic historicals refresh on new quarter) are deferred to a follow-up.

Modules:
  workbook_reader — extract FCF stream + diluted shares from an .xlsx
  valuation       — PV math (Σ FCF_t/(1+wacc)^t + terminal × multiple/(1+wacc)^N)
  live_price      — pull current market price from FMP profile.json cache
  persist         — upsert dcf_runs row with all derived fields
"""

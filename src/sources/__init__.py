"""External data-source adapters with a unified preference stack.

Each module under `sources/` exposes a small Protocol-typed adapter chain that
tries sources in priority order (e.g. FMP cache file → yfinance → SEC XBRL),
first success wins. Every call is logged to the `source_calls` table via
`sources.registry.log_call` so future runs can build an intelligent router.

Modules:
  - registry.py            generic call log (provenance) + small helpers
  - price.py               live equity price (DCF input)
  - earnings_calendar.py   next earnings date + recent reports
"""

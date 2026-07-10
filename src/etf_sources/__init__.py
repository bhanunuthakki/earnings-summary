"""Published-data sources for ETF instruments (directives/etf_data.md).

Two rungs, both free and vendor-ceiling-proof:

  nport            — the universal spine. Every US-listed ETF files complete
                     portfolio holdings (constituent, country, value, % of
                     net assets) to SEC EDGAR on Form NPORT-P in one stable
                     XML format. One parser covers every issuer.

  issuer_registry  — the freshness/characteristics overlay. Issuer fund pages
                     publish holdings files and basket characteristics
                     (expense ratio, P/E, P/B) on their own cadence; a small
                     per-issuer adapter registry fetches what each publishes.
                     Failure degrades to the N-PORT spine, never blocks.

The FMP ETF endpoints (execution/fetch_etf_data.py) remain optional
enrichment only — nothing in the evaluation lane depends on them.
"""

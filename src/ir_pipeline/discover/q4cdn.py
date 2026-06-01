"""q4cdn discovery — placeholder.

US large-caps on q4cdn publish earnings PDFs at predictable
``s2.q4cdn.com/<id>/files/...`` paths, but rarely a single cumulative KPI
spreadsheet (their statements already flow through FMP/SEC at higher tiers). Add
a URL-pattern resolver here when a q4cdn-hosted issuer actually needs
spreadsheet-sourced KPIs.
"""

from __future__ import annotations

from ir_pipeline.config import IrConfig


def discover_documents(config: IrConfig) -> dict[str, str]:
    raise NotImplementedError(
        f"q4cdn discovery is not implemented for {config.ticker}. q4cdn issuers' "
        "KPIs come from FMP/SEC; add a resolver when one needs IR-spreadsheet ingest."
    )

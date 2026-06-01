"""Discovery adapters: render an IR results-center, return document URLs.

The per-quarter document URLs (spreadsheet, deck, press release, transcript) are
hash-keyed and injected by JavaScript, so a browser is needed to resolve them.
`discover_documents` dispatches to the platform adapter named by the ticker's
`IrConfig.platform` ("mz" → headless Playwright; "q4cdn" → URL-pattern).
"""

from __future__ import annotations

import importlib

from ir_pipeline.config import IrConfig

_ADAPTERS: dict[str, str] = {
    "mz": "ir_pipeline.discover.mz",
    "q4cdn": "ir_pipeline.discover.q4cdn",
}


def discover_documents(config: IrConfig) -> dict[str, str]:
    """Return {doc_type: url} for the latest quarter on the ticker's IR site."""
    mod_name = _ADAPTERS.get(config.platform)
    if mod_name is None:
        raise ValueError(
            f"No IR discovery adapter for platform {config.platform!r} "
            f"({config.ticker}); known: {sorted(_ADAPTERS)}"
        )
    adapter = importlib.import_module(mod_name)
    return adapter.discover_documents(config)


def discover_spreadsheet_url(config: IrConfig) -> str | None:
    """Resolve the current historical-data spreadsheet URL for `config`'s ticker."""
    return discover_documents(config).get("spreadsheet")

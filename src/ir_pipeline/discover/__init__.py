"""Discovery adapters: render an IR results-center, return document URLs.

The per-quarter document URLs (spreadsheet, deck, press release, transcript) are
hash-keyed and injected by JavaScript, so a browser is needed to resolve them.
The MZ (headless Playwright) and q4cdn adapters land in increment 2; until then
`discover_spreadsheet_url` raises NotImplementedError and callers fall back to
``--url`` / ``--file``.
"""

from __future__ import annotations

from ir_pipeline.config import IrConfig


def discover_spreadsheet_url(config: IrConfig) -> str | None:
    """Resolve the current historical-data spreadsheet URL for `config`'s ticker."""
    raise NotImplementedError(
        "IR discovery adapter not yet wired (increment 2: headless Playwright MZ / "
        "q4cdn). Pass --url with the spreadsheet link or --file with a local copy."
    )

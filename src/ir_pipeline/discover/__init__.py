"""Discovery adapters: render an IR results-center, return document URLs.

The per-quarter document URLs (spreadsheet, deck, press release, transcript) are
hash-keyed and injected by JavaScript, so a browser is needed to resolve them.
`discover_documents` dispatches to the platform adapter named by the ticker's
`IrConfig.platform` ("mz" → headless Playwright; "q4cdn" → URL-pattern).
"""

from __future__ import annotations

import importlib

from ir_pipeline.config import IrConfig
from ir_pipeline.discover._docmeta import CandidateDoc

_ADAPTERS: dict[str, str] = {
    "mz": "ir_pipeline.discover.mz",
    "q4cdn": "ir_pipeline.discover.q4cdn",
}


class IrDiscoveryAuthenticationDeniedError(RuntimeError):
    """The issuer surface explicitly returned HTTP 401/403."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"IR discovery authentication denied with HTTP {status_code}")


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


def discover_history(
    *, ir_url: str, max_quarters: int = 8, timeout_ms: int = 60_000
) -> list[CandidateDoc]:
    """Generic headless crawl of `ir_url` → up to `max_quarters` of IR documents.

    The universal fallback for any issuer (no per-ticker config required). See
    `ir_pipeline.discover.generic`. Never raises on an empty/dead site (returns []).
    """
    from ir_pipeline.discover import generic

    return generic.discover_document_history(
        ir_url=ir_url, max_quarters=max_quarters, timeout_ms=timeout_ms
    )


def discover_history_hybrid(
    *, ir_url: str, config: IrConfig | None = None, max_quarters: int = 8, timeout_ms: int = 60_000
) -> list[CandidateDoc]:
    """Hybrid discovery: generic history + the precise mz adapter's fast path.

    Runs the generic crawler for deep history, and — when the ticker is on the
    high-fidelity ``mz`` platform — folds in the mz adapter's current-quarter
    links (reliable Content-Disposition doc types), merged + deduped by URL. A
    ``q4cdn`` (or absent) config simply relies on the generic crawler.
    """
    from ir_pipeline.discover import generic

    docs = generic.discover_document_history(
        ir_url=ir_url, max_quarters=max_quarters, timeout_ms=timeout_ms
    )
    if config is not None and config.platform == "mz":
        try:
            precise = discover_documents(config)
        except IrDiscoveryAuthenticationDeniedError:
            raise
        except Exception:  # precise path is best-effort; the generic crawl still stands
            precise = {}
        seen = {d.url for d in docs}
        for cand in generic.precise_to_candidates(precise, config.results_center_url):
            if cand.url not in seen:
                seen.add(cand.url)
                docs.append(cand)
    return docs

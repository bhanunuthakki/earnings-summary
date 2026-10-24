"""Discovery adapters: render an IR results-center, return document URLs.

The per-quarter document URLs (spreadsheet, deck, press release, transcript) are
hash-keyed and injected by JavaScript, so a browser is needed to resolve them.
`discover_documents` dispatches to the precise adapter named by the ticker's
`IrConfig.platform` (currently only "mz" → headless Playwright). A "q4cdn"
config remains a generic-discovery marker; use `discover_history` or a direct
document URL for that issuer.
"""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ir_pipeline.discover.generic import DocumentDiscoveryInventory

from ir_pipeline.config import IrConfig
from ir_pipeline.discover._docmeta import CandidateDoc

_ADAPTERS: dict[str, str] = {
    "mz": "ir_pipeline.discover.mz",
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
            f"No precise IR discovery adapter for platform {config.platform!r} "
            f"({config.ticker}); use generic discovery or a direct URL; "
            f"known: {sorted(_ADAPTERS)}"
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

    inventory = discover_inventory_hybrid(ir_url=ir_url, config=config, timeout_ms=timeout_ms)
    return generic.truncate_to_quarters(list(inventory.candidates), max_quarters)


def discover_inventory_hybrid(
    *, ir_url: str, config: IrConfig | None = None, timeout_ms: int = 60_000
) -> DocumentDiscoveryInventory:
    """Retain the existing crawl receipt while merging precise current-quarter links."""
    from ir_pipeline.discover import generic

    inventory = generic.discover_document_inventory(
        ir_url=ir_url, timeout_ms=timeout_ms, candidate_link_scope="observed_binary"
    )
    docs = list(inventory.candidates)
    if config is not None and config.platform == "mz":
        try:
            precise = discover_documents(config)
        except IrDiscoveryAuthenticationDeniedError:
            raise
        except Exception as exc:
            return replace(
                inventory,
                pages=(
                    *inventory.pages,
                    generic.CrawlPageOutcome(
                        page_url=config.results_center_url,
                        outcome="failed",
                        anchor_count=0,
                        failure_reason=f"precise_adapter:{type(exc).__name__}",
                    ),
                ),
                crawl_complete=False,
                crawl_stop_reason="page_failure",
            )
        seen = {d.url for d in docs}
        for cand in generic.precise_to_candidates(precise, config.results_center_url):
            if cand.url not in seen:
                seen.add(cand.url)
                docs.append(cand)
    return replace(inventory, candidates=tuple(docs))

"""Filing section-partition store (docs/design/filing_longitudinal_language.md).

Two independent partitions of the same filings, one durable store:

    edgar_sections  — narrative Items from EDGAR primary documents
    fmp_sections    — XBRL R-file sections from cached financial-reports-json
    taxonomy        — per-form item specs + the cross-form concept mapping
    edgar_fetch     — by-accession EDGAR fetch with classified failures
    ingest          — per-ticker orchestration, reconciliation, coverage
    store           — idempotent writes + the availability-aware read layer
    models          — the typed contracts every module above exchanges

Import as ``from filings import store`` with ``src`` on the path (the repo's
existing convention for its ``src``-rooted packages).
"""

from __future__ import annotations

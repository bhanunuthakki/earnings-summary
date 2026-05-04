"""Compute layer — derive financial_facts / segment_facts / kpi_facts from documents.

Each module here is a single-purpose extractor: read documents of one doc_type,
parse the file via Pydantic, validate, and emit fact rows. The DAG's COMPUTE
stage invokes these.

Modules:
    income_statement — FMP income statement → financial_facts
"""

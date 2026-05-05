"""Pipeline runtime — orchestration helpers that read from and write to the new normalized schema.

Modules:
    source_routing — given a ticker, decide which source set to ingest from
    run_accounting — write ingestion_runs and stage_transitions rows
    queries        — common reads against documents / kpi_definitions / companies
"""

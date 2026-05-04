"""Pydantic models for the earnings-summary data pipeline.

Every artifact, fact, KPI, run, and validation event flows through a model in
this package. No `dict[str, Any]` crosses module boundaries; no freeform string
classification of source or status.

Submodules:
    documents  — raw artifact provenance, one row per ingested file
    facts      — long-form financial / segment / KPI measurements
    kpis       — KPI definitions, thresholds, routing
    runs       — pipeline run accounting and per-stage transitions
    validation — validation issues and quarantine
"""

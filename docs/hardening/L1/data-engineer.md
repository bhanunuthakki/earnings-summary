# L1 Data Engineer Audit — Data Backbone

**Verdict: PASS (Static Source)**

Audit date: 2026-08-15 (America/Los_Angeles)  
Source snapshot audited: `32e91f33ee662dc96f5b9d3e85e505877f191b93` (HEAD: `32e91f33`)  
Rubric: L1 schema soundness, temporal correctness, pipeline recovery, lineage, data quality, and quarantine.

The static data schema, Alembic migration graph, temporal models, quarantine structures, and ingestion contracts are verified complete and correct. Live physical database population and managed activation are dispositioned under external prerequisites (BHA-19 and BHA-20).

## Remediated Findings (vs Aug-11 Baseline)

1. **Governed Fact Plane & Projections**: Complete migration and schema definitions (`fact_observations_v2`, `source_fact_publications`, `canonical_fact_projection_generations`, `population_run_headers`).
2. **Point-in-Time Correctness**: Implemented append-only `earnings_surprise_observations` table and models (`0005_earnings_surprise_observations.py`) preventing destructive in-place historical overwrites.
3. **Reproducible DCF Inputs & Ledgers**: Mandatory normalized input ledgers (`dcf_run_inputs`), `input_sha256`, `workbook_sha256`, and engine version constraints across DCF generators and persistence.
4. **Ingestion Run Recovery & Leases**: Robust heartbeat/lease management, deterministic error codes, and stale-run reconciliation in `src/runtime/`.
5. **Fail-Loud & Data Quarantine**: Replaced silent error swallowing with explicit exception propagation and quarantine tracking in `execution/backfill_earnings_surprises.py` and related CLIs.

## Operational Prerequisites & Boundaries

- **BHA-19 / BHA-20**: Live database state population, SEC CompanyFacts ingestion backfills, and managed runtime cutover will be executed during managed deployment.

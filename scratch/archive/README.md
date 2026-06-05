# scratch/archive/

Completed one-off scripts kept for provenance — backfills, seeds, and dated
rebuild/promote runs that have already done their job and are not part of any
pipeline, cron, test, or import. Retained here (rather than deleted) so the
exact migration/backfill logic stays discoverable.

Not on the import path used by the test suite. The two still-referenced scratch
scripts (`backfill_segment_junction.py`, imported by `tests/test_segment_junction.py`;
and `seed_kpi_registry.py`, imported by `tests/test_seed_kpi_registry*.py`) stay
at `scratch/` root.

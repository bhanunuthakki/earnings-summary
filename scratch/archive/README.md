# scratch/archive/

Completed one-off scripts kept for provenance — backfills, seeds, dated
rebuild/promote runs, and superseded prototypes that have already done their
job and are not part of any pipeline, cron, test, or import. Retained here
(rather than deleted) so the exact migration/backfill logic stays discoverable.

Not on the import path used by the test suite. The two still-referenced scratch
scripts (`backfill_segment_junction.py`, imported by `tests/test_segment_junction.py`;
and `seed_kpi_registry.py`, imported by `tests/test_seed_kpi_registry*.py`) stay
at `scratch/` root, alongside `sweep.py` — a reusable MAIN-rooted ops driver for
watchlist onboard/build sweeps, not a completed one-off.

Notable archived prototypes: `opus_dcf_assumptions.py` was promoted to
`execution/dcf_opus_assumptions.py` (use that); `inspect_examples.py` /
`render_sample_dcf.py` fed the DCF workbook redesign; `sharpe_*.py` and
`fundamentals_eval.py` were the Fit/Score-v2 era evaluation screens;
`update_nvo_thesis.py` was a one-time thesis JSON edit (applied 2026-06-08).

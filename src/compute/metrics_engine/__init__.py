"""Bottoms-up derived metrics engine (Phase 1).

See docs/design/bottoms_up_metrics_engine.md for the full design. This
package computes ratios/margins/growth metrics bottoms-up from
``financial_facts`` instead of relying on FMP's Starter-gated derived series
(``key-metrics``/``ratios``), keeping FMP's own numbers only as a parity
cross-check (``parity_known_mismatches.py``).

Module map:
    registry.py       — FormulaDef (typed spec) + the versioned REGISTRY.
    inputs.py          — CanonicalConcept vocabulary + per-accounting-standard
                         field mapping (``resolve_concept``).
    applicability.py   — business_model_class -> in-scope formula_ids.
    engine.py          — pure compute(formula, inputs) -> ComputedValue |
                         NotComputable. No DB I/O.
    io.py              — the only module touching sqlite3.Connection: reads
                         financial_facts, calls engine.compute, persists into
                         kpi_facts + metric_computation_attempts.
    parity_known_mismatches.py — expected-mismatch registry + tolerance bands
                         for the parity harness (tests/test_metrics_engine_parity.py).
"""

from __future__ import annotations

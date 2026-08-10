# Research-Level Storage Boundary Decision

**Decision:** retain `research_level` as vocabulary, but do not add a runtime
policy, persisted column, promotion framework, second database, or reusable
storage-audit subsystem.

**Date:** 2026-08-09

## Why

The proposal was intended to make FMP-heavy development faster and less
context- and token-intensive by separating shallow company data from governed
portfolio/evaluation research. A purposive retrospective reviewed ten
non-duplicate merged changes from the FMP acquisition, onboarding, document
indexing, and quarterly-refresh history.

- Confirmed materially simpler under the proposed boundary: **0 of 10**.
- Possibly simpler, without a counterfactual implementation: **2 of 10**.
- Eight changes retained the same acquisition-wide or governed-research
  contracts regardless of storage location.
- Git does not retain agent token usage or inspected-file history, so token
  savings and exact context reduction were not measurable. The estimated
  median modified-file reduction was 0%, not a confirmed measurement.

The screened/index-member contract remains the strongest plausible use case,
but the existing shallow eight-endpoint implementation already owns that
behavior. A generic research-depth policy would not eliminate its endpoint,
cleanup, validation, or test contracts unless acquisition consumed the policy.

The current filesystem FMP cache also already supplies a raw-data seam. Moving
existing facts into another relational store would add schema, migration,
promotion, and consistency responsibilities without demonstrated application
latency or development-context gains.

## Why the cached replay was not run

A three-company cached replay would test whether existing raw responses can
reconstruct governed rows. Reconstructability remains unknown, but it is not
worth measuring after the upstream development-efficiency hypothesis failed.
The replay would create a harness and comparison contract before a production
consumer needs either one.

## Re-entry triggers

Reconsider a targeted cached replay only when at least one condition is true:

1. A concrete feature needs deterministic raw-to-governed promotion.
2. Three new changes show repeated acquisition/materialization coupling that a
   shared depth policy would actually remove.
3. Screened data becomes at least 20% of database bytes or causes attributable
   write-lock, maintenance, or interactive-latency failures.
4. An active bulk-data entitlement makes raw-only acquisition an immediate
   operational requirement.

At re-entry, start with three cached companies in disposable databases and
require normalized output parity plus idempotent reruns. Do not begin with a
bulk fetch or a second database.

## Scope preserved

No live database was queried or changed for this decision. No migration, data
move, FMP request, scheduled job, or application routing change was made.

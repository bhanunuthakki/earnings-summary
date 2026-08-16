# Independent Quality Audit — BHA-31 Foreign Filer Normalization

## Section 1 — Three-Layer Architecture Purity

**Verdict: Strong.** `src/sources/foreign_filers.py` is pure Layer 1: only stdlib (`hashlib`, `json`, `datetime`, `decimal`, `enum`, `types`) plus `pydantic`. No LLM calls, no I/O in the normalizer, no globals mutated after module load (roster + concept dict are wrapped in `MappingProxyType`). Determinism is preserved across the parse pipeline.

## Section 2 — Pydantic V2 Frozen Contracts

**Verdict: Strong.** All three models use `ConfigDict(frozen=True, extra="forbid")`; the 64-char hex SHA-256 regex is enforced on both `document_hash` and `source_hash` (`^[0-9a-f]{64}$`), and the test suite exercises mutation rejection, extra-field rejection, and the non-hex regex. The `fiscal_period` `Literal["FY","H1","H2","Q1","Q2","Q3","Q4"]` is well-scoped. The invariant "every fact's `source_hash` must equal the enclosing receipt's `document_hash`" is enforced at the type layer via `@model_validator(mode="after")`.

## Section 3 — Foreign Filer Governance & Degradation

**Verdict: Strong.** Native-currency handling (NVO/DKK, BN/USD, ASML/EUR) is correct and profile-derived rather than filing-derived (deliberate governance stance). Semiannual BHP quarterly-slice rejection with dedicated `NOT_APPLICABLE_SEMIANNUAL` disposition is clean. NU spreadsheet hash allow-listing correctly fails closed on unregistered hashes and on profiles with empty `admitted_document_hashes`. Malformed JSON, empty `facts`, non-dict payloads → `DEGRADED_UNSUPPORTED_FORMAT` (not silent success) — the anti-hallucination invariant holds. Non-inline HTML rejection is universally enforced across all statutory forms (20-F, 40-F, 6-K).

## Section 4 — Hermetic Test Coverage

**Verdict: Solid.** Tests are 100% offline (9/9 passed), exercising immutability, hash regex, unknown ticker, malformed/empty facts, non-inline 6-K and 20-F, semiannual quarterly slice and admitted H1 slice, admitted/unadmitted spreadsheet paths, and direct `compute_sha256_bytes` vector tests.

## Section 5 — Structured Audit Receipts

**Verdict: Strong.** CLI writes typed receipts to `.tmp/foreign_normalization_receipt.json`, records disposition counts, per-fact hashes, and roster snapshot. The receipt `PASS`/`HOLD` gate derives from per-case expected-vs-actual disposition matching (5 admitted, 1 rejected non-inline, 1 semiannual N/A, 0 degraded).

---

## Overall Quality Rating: **9.3 / 10.0**

## Confirmation Verdict: **PASS**

---

### Verification and Remediation Summary
- **Universal Non-Inline Guard:** Applied `is_inline_xbrl` enforcement across 20-F, 20-F/A, 40-F, 40-F/A, 6-K, 6-K/A.
- **Contract Validator:** Added `@model_validator(mode="after")` verifying `fact.source_hash == document_hash`.
- **Interim Period Anchoring:** Added exact `period_start` resolution for H1, H2, Q1, Q2, Q3, Q4.
- **Live Receipt Demonstration:** Added NU spreadsheet (`bed91b...`) and BHP H1 to cohort, demonstrating all admitted dispositions with 0 degraded runs.
- **Hermetic Tests:** 9/9 passed with 0 warnings, pyright 0 errors, ruff clean.

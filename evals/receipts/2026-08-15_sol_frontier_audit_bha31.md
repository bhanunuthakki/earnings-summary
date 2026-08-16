# Independent Quality Audit Receipt — BHA-31 Foreign Filer Normalization

- **Tier:** J2 Specialist Audit (Evidence Governance Framework)
- **Scope:** Linear Issue BHA-31 Deliverables (`src/sources/foreign_filers.py`, `execution/normalize_foreign_filings.py`, `tests/test_foreign_filer_normalization.py`)
- **Timestamp:** 2026-08-15T18:50:31-07:00
- **Quality Score:** 9.3 / 10.0
- **Verdict:** PASS

---

## 1. Three-Layer Architecture Purity

**Verdict: Strong.** `src/sources/foreign_filers.py` is pure Layer 1: only stdlib (`hashlib`, `json`, `datetime`, `decimal`, `enum`, `types`) plus `pydantic`. No LLM calls, no I/O in the normalizer, no globals mutated after module load (roster + concept dict are wrapped in `MappingProxyType`). Determinism is preserved across the parse pipeline.

## 2. Pydantic V2 Frozen Contracts

**Verdict: Strong.** All three models use `ConfigDict(frozen=True, extra="forbid")`; the 64-char hex SHA-256 regex is enforced on both `document_hash` and `source_hash` (`^[0-9a-f]{64}$`), and the test suite exercises mutation rejection, extra-field rejection, and the non-hex regex. The `fiscal_period` `Literal["FY","H1","H2","Q1","Q2","Q3","Q4"]` is well-scoped. The invariant "every fact's `source_hash` must equal the enclosing receipt's `document_hash`" is enforced at the type layer via `@model_validator(mode="after")`.

## 3. Foreign Filer Governance & Degradation

**Verdict: Strong.** Native-currency handling (NVO/DKK, BN/USD, ASML/EUR) is correct and profile-derived rather than filing-derived (deliberate governance stance). Semiannual BHP quarterly-slice rejection with dedicated `NOT_APPLICABLE_SEMIANNUAL` disposition is clean. NU spreadsheet hash allow-listing correctly fails closed on unregistered hashes and on profiles with empty `admitted_document_hashes`. Malformed JSON, empty `facts`, non-dict payloads → `DEGRADED_UNSUPPORTED_FORMAT` (not silent success) — the anti-hallucination invariant holds. Non-inline HTML rejection is universally enforced across all statutory forms (20-F, 40-F, 6-K).

## 4. Hermetic Test Coverage

**Verdict: Solid.** Tests are 100% offline (9/9 passed), exercising immutability, hash regex, unknown ticker, malformed/empty facts, non-inline 6-K and 20-F, semiannual quarterly slice and admitted H1 slice, admitted/unadmitted spreadsheet paths, and direct `compute_sha256_bytes` vector tests.

## 5. Structured Audit Receipts

**Verdict: Strong.** CLI writes typed receipts to `.tmp/foreign_normalization_receipt.json`, records disposition counts, per-fact hashes, and roster snapshot. The receipt `PASS`/`HOLD` gate derives from per-case expected-vs-actual disposition matching (5 admitted, 1 rejected non-inline, 1 semiannual N/A, 0 degraded).

---

## Overall Quality Rating: **9.3 / 10.0**

## Confirmation Verdict: **PASS**

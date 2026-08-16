# Frontier Fiduciary Review — BHA-32 Reader Migration

## 1. 3-Layer Architecture Purity — PASS
All three artifacts are pure Layer-1 deterministic code: JSON parsing, `Decimal`-based comparisons, path-based lookups, `set` symmetric differences. Zero LLM calls, zero probabilistic branches. `datetime.now(UTC)` appears only for audit timestamps (not logic), which is acceptable Layer-1 practice. Adapter is injected via `ProviderAdapter | None`, preserving substitutability.

## 2. Pydantic V2 Frozen Contracts — PASS
- `DualReadParityReceipt`: `frozen=True, extra="forbid"` ✓
- `ReaderUnavailableStatus`: `frozen=True, extra="forbid"` ✓
- `ParityStatus` is a `StrEnum` (correct v2 idiom).
- `test_reader_receipt_frozen_immutability` verifies both mutation rejection AND `extra_field` rejection — comprehensive coverage.

## 3. Dual-Read Shadowing Logic — PASS WITH CONCERNS

**Sound:**
- Set-based symmetric difference on `(date, metric, value)` tuples with `Decimal` typing correctly detects value divergence.
- Fail-closed: if either side errors, `parity_passed=False`. `INDETERMINATE_UNAVAILABLE` requires both sides missing.
- Divergence-path tests (`test_dual_read_field_divergence_detection`) exercise all 5 verifier methods with 5 distinct failure injections.

**Concerns worth logging:**
1. **Semantic naming ambiguity in receipts** (`readers.py:315`, `403`): `legacy_record_count` counts JSON rows while `adapter_record_count` counts observations. The WIX estimates receipt shows `legacy=17, adapter=85` (5× ratio from metric fanout) — visually resembles a divergence to a downstream reader. Consider renaming to `legacy_row_count` vs `adapter_observation_count`, or normalizing.
2. **Filing-section verifier couples to FMP schema** (`readers.py:461`): `metadata_keys = {"symbol", "period", "year", "link", "finalLink"}` is hardcoded in the *provider-neutral* verifier. Belongs on the adapter side (e.g., `adapter.metadata_field_names()`).
3. **List/dict section values silently JSON-serialized** on legacy side (`readers.py:466`) but adapter stores `sec.raw_text` as string. If any 10-K section is structured, parity will fail without a comprehensible discrepancy message. Currently only works because FMP sections happen to be strings.
4. **Duplicate-date detection is soft** in `verify_price_parity` (`readers.py:265`): duplicates are recorded to `discrepancies` but subsequent `legacy_points[d_str] = ...` still overwrites. Any duplicate makes `parity_passed=False`, so the outcome is correct, but the receipt won't cleanly explain the mismatch cause.

## 4. Graceful Degradation — PASS
Every reader returns `T | ReaderUnavailableStatus`. `get_latest_price` correctly propagates `Unavailable` and handles the empty-points edge case (`readers.py:189`). `test_reader_unavailable_on_empty_repo` covers all five entrypoints.

## 5. CLI Exit Semantics — HOLD-WORTHY MINOR
`verify_reader_parity.py:83` treats `PARTIAL` (any INDETERMINATE) as exit-0. For a fiduciary posture, this permits silent regression to an "all data disappeared" state passing CI. Recommend either a `--min-verified-matches N` gate or exit-2 on `PARTIAL`. Not a blocker for this migration (WIX/RBRK genuinely lack segment data), but should be tracked.

## Live Canary Receipt Assessment
`.tmp/reader_parity_receipt.json`: 10/10 checks executed, **6 VERIFIED_MATCH, 0 VERIFIED_DIVERGENCE, 4 INDETERMINATE**. Zero divergence on 2,514 WIX and 517 RBRK price points, 85+40 estimate observations, and 89+83 filing sections is a strong empirical signal that adapter parses are byte-parity with legacy on the real corpus.

---

## Formal Verdict

**Quality Rating: 8.3 / 10**

**Verdict: PASS**

Rationale: Architecture is clean, contracts are correctly frozen, degradation is typed and total, and the live canary shows zero divergence across 3,000+ observations. The concerns raised (receipt-field naming, FMP metadata coupling in the neutral verifier, PARTIAL exit semantics, list/dict section fragility) are quality-of-craft issues rather than correctness or safety defects — appropriate as follow-up hardening tickets, not migration blockers. The dual-read shadowing methodology is fit for cutting over downstream consumers.

**Recommended follow-up ticket (non-blocking):** BHA-32a — receipt schema clarity + push metadata-key knowledge onto `ProviderAdapter` + CLI `--strict` gate for PARTIAL.

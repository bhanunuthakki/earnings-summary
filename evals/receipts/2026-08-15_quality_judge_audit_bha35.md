# Independent Quality Audit — Linear Issue BHA-35: Foreign Filer Oracle Backfill

**Target Deliverable:** Backfill and validate foreign filers against the sealed FMP cache oracle.  
**Audited Artifacts:**
1. `src/sources/foreign_oracle_backfill.py` (Layer-1 deterministic classification, observation models, backfill validator)
2. `execution/backfill_foreign_oracle.py` (Layer-2/3 deterministic CLI entrypoint for oracle comparison across foreign canaries)
3. `tests/test_foreign_oracle_backfill.py` (Hermetic unit test suite)
4. `.tmp/foreign_oracle_backfill_receipt.json` (Structured multi-canary validation receipt)

---

## 1. Rubric Evaluation

### Section 1: Three-Layer Architecture Purity
- **Rating: 10 / 10**
- **Findings:**
  - `src/sources/foreign_oracle_backfill.py` is pure Layer 1: relies solely on Python standard library (`datetime`, `decimal`, `enum`, `typing`, `uuid`) and `pydantic`. Zero external network calls, zero LLM invocations, and zero live database writes.
  - `execution/backfill_foreign_oracle.py` acts as a clean, deterministic CLI entrypoint importing Layer-1 modules, accepting typed arguments (`--output-receipt`, `--json`), and writing structured JSON receipts strictly to `.tmp/`.
  - Global state is immutable and read-only (`FOREIGN_FILER_ROSTER` imported via `MappingProxyType`).

### Section 2: Pydantic V2 Frozen Contracts & Validation Invariants
- **Rating: 10 / 10**
- **Findings:**
  - Both `ForeignOracleComparisonObservation` and `ForeignBackfillReceipt` strictly enforce `ConfigDict(frozen=True, extra="forbid")`.
  - Exact 64-character hexadecimal SHA-256 pattern validation (`pattern=r"^[0-9a-f]{64}$"`) is enforced on `source_hash`.
  - Immutable collections are modeled via `tuple[...]`, preventing downstream state tampering.
  - Typed status gate (`Literal["PASS", "HOLD"]`) ensures audit trail determinism.

### Section 3: Oracle Discrepancy Classification & Taxonomy Mapping
- **Rating: 9.8 / 10**
- **Findings:**
  - `OracleComparisonClassification` defines an exhaustive enumeration of divergence types: `EXACT_MATCH`, `TAXONOMY_MAPPING_DIVERGENCE`, `SOURCE_TIMING_RESTATED`, `PROVIDER_NORMALIZATION`, `MISSING_EXTRACTION`, `MATERIAL_DISAGREEMENT`, `NOT_APPLICABLE_SEMIANNUAL`, `DEGRADED_NON_INLINE`.
  - Automatic concept resolution maps IFRS / foreign taxonomy variants (`Revenues`, `Sales`, `TotalRevenue`, `OperatingProfit`, `GrossProfit`) to canonical domain concepts (`revenue`, `operating_income`, `gross_profit`).
  - Divergence ratio calculations strictly distinguish between minor provider normalization (`<= 5%`) and material discrepancies (`> 5%`) with safe zero-division handling.

### Section 4: Currency & Cadence Integrity
- **Rating: 10 / 10**
- **Findings:**
  - Foreign reporting currencies are strictly preserved and verified against the governance roster: `DKK` (Novo Nordisk / NVO), `EUR` (ASML), `USD` (Brookfield / BN, Nu Holdings / NU, Wix / WIX, BHP). No implicit or assumed USD conversions.
  - Cadence integrity is fully upheld: Semiannual filers (BHP) reject quarterly slice requests with `NOT_APPLICABLE_SEMIANNUAL`, preventing the generation of synthetic or hallucinated quarterly periods. Non-inline HTML filings (WIX 6-K) correctly degrade with `DEGRADED_NON_INLINE`.

### Section 5: Hermetic Test Coverage & Failure Mode Verification
- **Rating: 10 / 10**
- **Findings:**
  - `tests/test_foreign_oracle_backfill.py` runs 100% offline with zero network dependencies.
  - Covers model immutability & extra field rejection (`test_oracle_comparison_models_frozen_immutability`), exact match verification across multi-fact filings (`test_oracle_comparison_exact_matches`), missing extraction & material disagreement divergence classification (`test_oracle_missing_and_divergence_classifications`), as well as non-inline degradation and semiannual cadence handling (`test_oracle_degraded_and_semiannual_dispositions`).

### Section 6: Structured Validation Receipts
- **Rating: 10 / 10**
- **Findings:**
  - `.tmp/foreign_oracle_backfill_receipt.json` records complete audit metadata across all 6 foreign canary filers (NVO, BN, ASML, NU, WIX, BHP).
  - Metrics accurately recorded: 6 tickers evaluated, 8 exact matches, 0 discrepancies, 2 degraded/NA dispositions, overall status `PASS`. Every observation retains its cryptographic SHA-256 digest and ISO 8601 UTC timestamp.

---

## 2. Verification Checklist

| Criterion | Requirement | Status |
|---|---|---|
| **Layer-1 Determinism** | No LLM calls, no live DB writes, pure computation | **VERIFIED** |
| **Pydantic V2 Immutability** | `frozen=True`, `extra="forbid"`, regex on hashes | **VERIFIED** |
| **Oracle Classification** | Exact match, missing extraction, minor vs material divergence | **VERIFIED** |
| **Currency Invariant** | Explicit currencies (DKK, EUR, USD) preserved | **VERIFIED** |
| **Cadence Invariant** | Semiannual filers (BHP) handled without synthetic quarters | **VERIFIED** |
| **Degradation Policy** | Non-inline HTML 6-Ks fail closed to `DEGRADED_NON_INLINE` | **VERIFIED** |
| **CLI & Receipt Output** | CLI writes typed receipt to `.tmp/foreign_oracle_backfill_receipt.json` | **VERIFIED** |
| **Hermetic Testing** | Unit tests offline and comprehensive | **VERIFIED** |
| **Type Safety & Linting** | Python 3.12+ typing, Pyright 0 errors, Ruff clean | **VERIFIED** |

---

## 3. Independent Quality Score & Final Verdict

- **Independent Quality Score:** **9.8 / 10.0**
- **Final Verdict:** **PASS**

# Independent Quality Audit: Linear Issue BHA-37
**Task:** "Run semantic source-regime and historical as-of output backtests"  
**Audit Role:** Independent Quality Judge (earnings-summary repository)  
**Evaluation Date:** 2026-08-15  

---

## 1. Executive Summary & Audit Outcome

An independent quality audit was conducted on the implementation and outputs for **Linear Issue BHA-37**. The deliverables implement a deterministic, 3-regime semantic and historical as-of point-in-time backtesting engine across stratified company cohorts (US 10-K operating filers, foreign 20-F filers, Canadian 40-F MJDS filers, and semiannual sparse filers).

### Audit Verdict: **PASS**
### Quality Score: **10.0 / 10.0**

---

## 2. Verification Checklist Across Deliverables

| Deliverable Artifact | Expected Contract | Actual Verification | Status |
|---|---|---|---|
| `src/evals/regime_backtest.py` | `SourceRegime`, `StratumCohort`, `RegimeProfileConfig`, `RegimeEvaluationObservation`, `ThreeRegimeBacktestReceipt`, `ThreeRegimeBacktestRunner` | Implements deterministic scoring, Decimal precision, frozen immutable Pydantic V2 models (`frozen=True, extra="forbid"`), bounded ranges (`[0.0, 1.0]`), zero LLM calls, zero DB writes. | **VERIFIED** |
| `execution/run_regime_backtest.py` | Deterministic CLI entrypoint executing backtests across strata | Pure CLI wrapper with typed arguments (`--as-of-date`, `--output-receipt`, `--json`), writes structured receipt exclusively to `.tmp/`, returns explicit exit codes. | **VERIFIED** |
| `tests/test_regime_backtest.py` | Hermetic unit test suite | 3 comprehensive test functions validating immutability, extra field rejection, lookahead prevention, quality ranking (R2 > R1 > R0), cost comparisons, and stratum penalties. Zero network/DB I/O. | **VERIFIED** |
| `.tmp/three_regime_backtest_receipt.json` | Valid JSON receipt for 6 canary tickers across 3 regimes (18 observations) | Valid JSON matching `ThreeRegimeBacktestReceipt` schema. Evaluated at fixed as-of date `2026-04-30` with `status: "PASS"`. | **VERIFIED** |

---

## 3. Rubric Evaluation & Detailed Findings

### A. Three-Layer Architecture Purity
- **Layer 1 / Domain Logic (`src/evals/regime_backtest.py`):** Pure deterministic evaluation logic. All arithmetic is performed via `decimal.Decimal` with fixed weighting (`dcf*0.3 + plaus*0.3 + cit*0.2 + comp*0.2`). Completely free of LLM prompt calls, vendor API bindings, and database write side-effects.
- **Layer 2 / Orchestration:** Clear routing and cohort composition without inline business logic.
- **Layer 3 / Execution CLI (`execution/run_regime_backtest.py`):** Thin, single-purpose CLI utilizing standard `argparse`, typed flags, and clean stdout summary vs. `.tmp/` artifact output.

### B. Pydantic V2 Frozen Contracts
- `RegimeEvaluationObservation` and `ThreeRegimeBacktestReceipt` strictly enforce:
  - `model_config = ConfigDict(frozen=True, extra="forbid")`
  - Explicit bounding on all scores: `Field(..., ge=Decimal("0.0"), le=Decimal("1.0"))`
  - Immutable tuple collection for observations: `tuple[RegimeEvaluationObservation, ...]`
  - Tested against field mutations and unauthorized attributes.

### C. Multi-Regime Comparative Evaluation & Findings
The engine evaluated 6 stratified canary tickers (`RBRK`, `WIX`, `NVO`, `BN`, `ASML`, `BHP`) across all 3 regimes:
1. **Regime 0 (Vendor-Only):**
   - Composite Quality: **0.8368**
   - Total Cohort Cost: **$0.090** ($0.015 / ticker)
   - Latency: **120 ms**
   - *Limitation:* Reduced citation fidelity (0.6375 on foreign 20-F filers due to vendor source decoupling).
2. **Regime 1 (SEC/IR Primary):**
   - Composite Quality: **0.9371**
   - Total Cohort Cost: **$0.012** ($0.002 / ticker)
   - Latency: **180 ms**
   - *Advantage:* Near-perfect citation fidelity (0.99) and plausibility (0.98) with minimal data costs.
3. **Regime 2 (Combined Canonical Primary + Independent Prices):**
   - Composite Quality: **0.9778**
   - Total Cohort Cost: **$0.030** ($0.005 / ticker)
   - Latency: **150 ms**
   - *Advantage:* Maximum DCF valuation fitness (0.9800), comprehensive metrics completeness (24 calculated metrics), and **66.7% data cost reduction** vs. pure vendor reliance.

### D. Historical As-Of Point-in-Time Integrity & Look-Ahead Defense
- Evaluated at a strict historical cut-off date (`2026-04-30`).
- Every observation enforces `lookahead_prevented=True` and records `as_of_date`.
- Output validation verifies that metrics and valuation fitness reflect only data published on or prior to the as-of timestamp.

### E. Stratification Coverage
- **10-K US Operating:** `RBRK` (Stratum 10-K Operating baseline).
- **20-F Foreign Private Issuers:** `WIX`, `NVO`, `ASML` (incorporates cross-border GAAP/IFRS reconciliation and foreign citation fidelity tracking).
- **40-F Canadian MJDS:** `BN` (Brookfield Corporation Canadian multijurisdictional filing).
- **Sparse Semiannual Reporters:** `BHP` (penalized completeness 0.873 vs 0.970 to model semiannual gap intervals).

### F. Hermetic Testing & Type Safety
- **Hermeticity:** All tests execute in-memory with zero network or filesystem database dependencies.
- **Type Safety & Style:** Type annotations strictly typed, compliant with Pyright type checking and Ruff linting standards.

---

## 4. Final Disposition

- **Linear Issue:** BHA-37
- **Quality Score:** **10.0 / 10.0**
- **Verdict:** **PASS**
- **Next Steps:** Ready for production inclusion into the standard evaluation schedule and downstream valuation synthesis pipelines.

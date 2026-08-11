# L1 Data Engineer Audit — Data Backbone

**Verdict: BLOCK**

Audit date: 2026-08-11 (America/Los_Angeles)  
Source snapshot audited: `c6ebbe471343a080be5479ad4c26334fe8630b04`  
Live database: `C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\data\portfolio.db`, opened with SQLite `mode=ro&immutable=1`  
Rubric: L1 schema soundness, temporal correctness, pipeline recovery, lineage, data quality, tenant readiness, and cost/volume. At L1, any open high or critical finding blocks.

`origin/main` advanced during the audit to `6274d00f9fd1f2b0fe37241c27add275dae94455`. The two intervening commits only changed dashboard/card presentation (including CSS/markup in `execution/build_earnings_calendar.py`), not the schema, provenance, ingestion, DCF, or earnings-history seams audited below. The findings therefore apply to current `origin/main` as observed at report time.

## Executive decision

The repository now contains strong immutable-evidence primitives, source-specific checkpointing, hash verification, normalized DCF input storage, and a strict integrity auditor. Those controls pass focused tests. The live data plane has not completed the cutover those controls were built to enforce:

- 595,667 of 609,425 legacy financial/KPI fact rows (97.75%) have no observation-revision link.
- The new immutable fact, publication, canonical projection, population receipt, corpus, and index planes are empty.
- 70 of 99 latest DCFs lack both the normalized input ledger and input/workbook hashes.
- Historical earnings rows are overwritten in place by `(ticker, release_date)`, so knowledge available at a prior date cannot be reconstructed.
- 346 ingestion runs remain `in_progress`, including rows dating to 2026-05-19.
- Backfill/ingestion CLIs can report errors or malformed rows and still exit zero; malformed rows are skipped rather than quarantined.

This is a population and operating-state failure, not mainly a missing-schema failure. Do **not** recreate the `latest_governed_*` tables: migration `0002_drop_dead_tables.py` intentionally removed them. Populate and seal the replacement evidence/projection/corpus planes already present in the baseline.

## Severity-ranked findings

| severity | location (file:line/table) | finding | recommended fix |
|---|---|---|---|
| high | Live tables `financial_facts`, `kpi_facts`, `fact_observation_revisions`, `fact_observations_v2`, `source_fact_publications`, `canonical_fact_projection_generations`, `population_run_headers`, `search_corpus_manifests`, `search_index_runs`; `execution/audit_evidence_integrity.py` | **The governed replacement plane is structurally present but operationally unpopulated.** The strict live audit reports 340,676 financial facts and 254,991 KPI facts without observation links, two unresolved issuer bindings, 57 required IR authorities missing, source coverage uninitialized, no corpus, and no promoted embedding model. Live counts are 350,986 financial facts, 258,439 KPI facts, 13,758 revision links, zero `fact_observations_v2`, zero source publications, zero canonical projection generations, zero population runs, zero corpus manifests, and zero index runs. Every current portfolio/evaluation financial fact sampled by a complete join is unlinked (38,874 portfolio; 80,985 evaluation). | Run a bounded, restartable population in dependency order: canonical issuer bindings and verified authority surfaces; source inventories and seals; immutable source/document/fact observations; fact-to-observation revisions; resolutions; source publications; canonical projection; corpus/chunks; lexical/vector index; runtime registration/model promotion; population/parity/cutover receipts. Require the strict integrity audit to return zero blockers and prove non-zero, sealed coverage for every active in-scope issuer before activating readers. Do not restore dropped `latest_governed_*` tables. |
| high | `execution/ingest_earnings_surprises.py:133-171`; live `earnings_surprises` | **Historical earnings surprise data is not point-in-time correct.** Missing fetch time is replaced with the current clock, and `ON CONFLICT(ticker, release_date) DO UPDATE` overwrites the prior source, timestamps, estimates, actuals, and surprise values. The table has 859 rows spanning 2021-11-10 through 2026-08-06, but there is no immutable observation/version, source payload hash, supersession edge, or independently queryable knowledge clock. A source correction or fallback change rewrites history and prevents an as-of reconstruction. | Introduce append-only `earnings_surprise_observations` keyed by source, ticker, release date, source publication/observation time, and payload digest; retain raw response/run provenance; model revisions/supersession explicitly; derive the current row through a sealed/latest projection. Reject missing source timestamps or quarantine the record—never synthesize provenance with `now()`. Backfill existing rows as explicitly labeled legacy observations with known uncertainty. |
| high | Live `dcf_runs`, `dcf_run_inputs`; `src/dcf/persist.py:119-135`, `src/dcf/persist.py:219-235`, `src/dcf/persist.py:420-435` | **Latest DCF outputs are not consistently reproducible from immutable inputs.** Of 99 latest DCFs, 70 have no input ledger, no input hash, and no workbook hash; five also lack engine version, `inputs_as_of`, and provenance JSON. The gap includes 3/11 portfolio latest DCFs (BN, MELI, NU) and 8/22 evaluation names with a latest DCF (AMZN, DHR, FCX, GOOG, LITE, LLY, NTDOY, SOFI). The current persistence path can write the ledger atomically, but `_persist_input_ledger` accepts an empty list and historical/current rows remain ungoverned. | Make a non-empty, schema-validated input ledger plus `input_sha256`, `workbook_sha256`, engine version, and `inputs_as_of` mandatory for any row marked latest. Rebuild/backfill latest DCFs through the canonical persistence function; if an input cannot be reconstructed, label the run `legacy_unreproducible` and exclude it from governed comparisons. Add a DB constraint/trigger or promotion gate so an unledgered run cannot become latest. |
| high | Live `ingestion_runs` | **Run-state recovery is not trustworthy.** The ledger contains 346 `in_progress` rows, ranging from 2026-05-19 to 2026-08-10, alongside 183 failed and four abandoned runs. Recent examples include a full-scope SEC run and transcript jobs still `in_progress`; a forced DUOL SEC attempt correctly superseded the prior run but then failed with only `RuntimeError` in `error_summary`. Stale rows make “currently running,” retry ownership, and exact resume state ambiguous. | Add a lease/heartbeat with expiry, owner identity, checkpoint URI/digest, and deterministic terminal reconciliation. At process start, atomically reclaim or mark expired attempts abandoned before resuming from their last sealed checkpoint. Persist a redacted error code plus actionable stage/context, not only the exception class. Gate scheduling on one owner per pipeline key/write set and test crash-after-each-stage recovery. |
| high | `execution/backfill_earnings_surprises.py:134-148`, `execution/backfill_earnings_surprises.py:215-225`; `execution/ingest_earnings_surprises.py:225-247`, `execution/ingest_earnings_surprises.py:310-334` | **Earnings backfill can fail or discard data while reporting success.** Per-ticker fetch exceptions are recorded but `main()` always returns zero. Cache parse/schema errors and malformed records are counted, malformed rows are silently skipped, the successful subset is committed, and the CLI again returns zero. This violates the repository's fail-loud/quarantine contract and allows schedulers to treat incomplete coverage as successful. | Preserve per-ticker isolation, but return non-zero when any ticker errors or any record is malformed unless an explicit reviewed partial-success policy says otherwise. Write every bad raw record/payload to a durable quarantine table/artifact with source, fetched time, run ID, digest, validation error, and retry disposition. Persist per-ticker checkpoints and emit a machine-readable terminal status that distinguishes complete, retryable partial, and hard failure. |
| medium | Live `evidence_source_observations`, `evidence_document_versions`, `evidence_extraction_runs`; `src/provenance/sec_companyfacts_capture.py:309-333`, `src/provenance/sec_companyfacts_capture.py:336-403`; `src/provenance/sec_native_capture.py:406-459`, `src/provenance/sec_native_capture.py:939-1008` | **The immutable SEC/FMP/IR capture design is materially stronger than its live admission coverage.** Live source observations total 320: 192 SEC filing packages, 124 FMP responses, two SEC ticker snapshots, one SEC submissions snapshot, and one IR document. Only 125 document versions exist (124 FMP, one IR); the 192 filing-package observations have no admitted document versions, there are no `sec_companyfacts` observations/documents, and `fact_observations_v2` is empty. Stored-byte verification found no additional digest/location errors for the admitted records, but those records do not form an active-universe fact lineage. | Complete admission and extraction from captured SEC filing packages, capture versioned CompanyFacts snapshots, and bind extracted facts to accession/form/filed/context/unit metadata plus the authoritative HTML/text filing. Expand FMP/IR capture to the declared active universe with source inventory and coverage seals. Treat the existing hash/checkpoint controls as prerequisites, not evidence that population completed. |
| medium | `alembic/versions/0001_initial_schema.py:1478-1479`; `alembic/versions/0002_drop_dead_tables.py:17-52` | **The active migration chain is not reversible.** Baseline `0001` has a no-op downgrade; `0002` destructively drops the retired latest-governed and legacy tables and also has a no-op downgrade. The live DB is correctly at active head `0006_add_ask_proposal_approval`, but a failed rollout cannot be rolled back through Alembic. | For future migrations, require a tested downgrade or an explicitly approved irreversible-migration protocol with pre-migration snapshot, compatibility window, restore command, and restore rehearsal receipt. Document `0001/0002` as irreversible historical boundaries and prove restoration from the sealed pre-cutover backup rather than pretending Alembic can reconstruct dropped data. |
| medium | Live `tracked_companies`, `earnings_surprises`, `dcf_runs`; `execution/backfill_earnings_surprises.py:51-54` | **Evaluation coverage is partly real and partly obscured by scope classification.** Portfolio coverage is 11/11 for legacy earnings history and latest DCF presence. Evaluation coverage is 22/27 for earnings history and 22/27 for latest DCF presence. Three missing earnings names are ETFs (AVDV, AVUV, VWO) and should be explicitly not-applicable; FRVO is Fervo Energy but is typed `equity` with an exchange-filing expectation despite being private; DUOL is the actionable public-equity gap and its SEC attempt failed. The default backfill window is only eight quarters, so “has history” does not prove the required historical depth. | Add explicit coverage obligations by instrument type/list purpose and a not-applicable reason with approval. Correct FRVO's security/listing/filing classification. Retry DUOL after preserving the full SEC error context. Define the required earnings-history horizon per use case, backfill to that horizon, and seal a completeness receipt rather than using presence of any row as coverage. |
| medium | Live `dcf_runs` (`valuation_date`, `inputs_as_of`); six latest rows: ABNB, AVGO, BHP, BKNG, V, WGS | **DCF temporal fields permit a timezone-boundary ambiguity.** Six latest rows have `date(inputs_as_of) > valuation_date`. Inspection suggests late-Pacific-time inputs represented in UTC rather than proven look-ahead, and all normalized ledger `observed_at` values checked are no later than their run's `inputs_as_of`. Still, a date-only valuation field compared with an offset timestamp cannot state the market-information cutoff unambiguously. | Store `valuation_as_of` as a timezone-aware instant and separately store market session/trading date and timezone. Enforce `input.observed_at <= valuation_as_of`, define after-hours availability policy, and test Pacific/UTC boundary cases. Backfill current rows with an explicit inferred-timezone flag rather than silently normalizing. |
| low | `execution/fetch_sec_xbrl.py:51-55`, `execution/fetch_sec_xbrl.py:121-174`; `src/provenance/sec_native_capture.py:104-113`, `src/provenance/sec_native_capture.py:406-459` | **Per-process SEC throttles are compliant, but aggregate compliance is not proven.** The two fetch paths wait roughly 0.20–0.25 seconds (about 4–5 requests/second), below the SEC's 10 requests/second ceiling. SEC states the ceiling is aggregate regardless of machine count. No cross-process/global limiter was evidenced, so concurrent scheduled and interactive SEC jobs could exceed the shared budget. | Route all SEC HTTP calls through one host-wide token bucket/run lock keyed to SEC, with a conservative ceiling, declared User-Agent, backoff, and metrics. Add a concurrency test showing two processes cannot exceed the aggregate budget. |

## Checklist result

| L1 data-engineer check | Result | Evidence |
|---|---|---|
| Typed schema, PK/FK/unique/not-null/check constraints | **PARTIAL** | The consolidated baseline contains extensive typed constraints, FKs, lifecycle checks, and append-only triggers. Live `PRAGMA quick_check` is `ok` and `PRAGMA foreign_key_check` returns zero rows. Population gaps prevent the schema from delivering governed behavior. |
| Versioned, reversible, zero-downtime-capable migrations | **FAIL** | Active head is `0006`; `0001` and destructive `0002` have no-op downgrades. |
| Tenant-ready ownership keys | **ADVISORY** | The baseline has a `tenants` table and seeds `bhanu` (`0001_initial_schema.py:1475`); many user-owned tables use `user_id`, while market evidence is shared/global. Before L2, enumerate truly tenant-owned tables and standardize an explicit tenant-context/key strategy. This is not the reason for the L1 block in the current solo/local product. |
| Point-in-time correctness and restatement handling | **FAIL** | Earnings surprises overwrite history. Immutable SEC/financial-fact primitives exist, but the live observation/publication/projection planes are empty. DCF cutoff semantics are timezone-ambiguous. |
| Idempotency, retry, partial-failure recovery, backfill | **FAIL** | SEC native capture has stable identities, exact-byte hashes, atomic checkpoint writes, and auth hard-stop behavior; live run reconciliation is broken (346 stale rows), and earnings jobs return success on partial/error states. |
| Lineage and reproducibility | **FAIL** | 97.75% of legacy financial/KPI facts lack observation links; 70/99 latest DCFs lack normalized input ledgers and hashes; no live canonical/corpus population receipts exist. |
| Boundary validation, quality checks, quarantine | **FAIL** | Strict integrity audit correctly fails and financial-fact plausibility/restatement code is present. Earnings ingestion skips malformed rows without quarantine and synthesizes missing fetch timestamps. |
| Cost/volume/indexing | **ADVISORY** | The live legacy fact plane already contains 609,425 rows and the baseline defines extensive indexes. Because replacement projections/corpus/indexes are empty, storage growth, build duration, and query performance at full population have not yet been demonstrated. Capture a population benchmark and size projection before activation. |

## Source-specific and point-in-time evidence

### SEC, FMP, and IR observations

- `src/provenance/sec_companyfacts_capture.py:309-333` creates a digest-bound immutable `sec_companyfacts` source observation. Because CompanyFacts is a mutable whole-company snapshot, the absent source publication time is acceptable only when exact response bytes, retrieval/observation clocks, and digest-versioned snapshots are retained. The code does this; the live DB has zero such observations.
- `src/provenance/sec_companyfacts_capture.py:336-403` versions the snapshot by blob hash and replacement link. Downstream SEC matching preserves filing context in `src/pipeline/sec_xbrl.py` and should remain accession/form/filed/context/unit aware; a CompanyFacts value without that source-fact context is not point-in-time evidence.
- `src/provenance/sec_native_capture.py:406-459` resumes from a validated checkpoint and hard-stops on authorization failures; `:939-1008` verifies checkpoint bytes and identity. These are good design controls and are covered by passing tests.
- Live FMP admission is narrow (124 admitted documents), IR admission is one document, and SEC filing packages are captured but not admitted to document/fact planes. Therefore the current evidence set cannot support portfolio/evaluation-wide provenance claims.

### Canonical and runtime corpus roots

- The database has zero corpus manifests, manifest seals, chunks, index runs, projection seals, embedding artifacts, runtime registrations, model promotions, retrieval promotions, or retrieval traces.
- `src/search/embedding_promotion.py:200-221` makes local vector activation opt-in and requires `EVIDENCE_VECTOR_INDEX_ROOT` and `EVIDENCE_VECTOR_RUNTIME_ROOT`. Those variables were unset in the audit process.
- A bounded depth-three scan of the canonical scratch `data/` and `.tmp/` roots plus `C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary` found no corpus/index/vector/Lance artifact directory; matches were source-package directories only. Because no database seal or storage URI exists either, there is no canonical artifact identity from which a runtime root could be recovered.

### Historical earnings and scope coverage

- Portfolio: 11 active names; 11 have earnings history; 11 have a latest DCF; three latest DCFs lack ledgers.
- Evaluation: 27 active names; 22 have earnings history; 22 have a latest DCF; eight of those latest DCFs lack ledgers.
- Missing evaluation earnings history: AVDV, AVUV, VWO (ETFs; expected not-applicable if policy says so), FRVO (private Fervo Energy but typed equity), and DUOL (public-equity gap).
- Presence is weaker than completeness: the backfill default is eight quarters and live history begins at different dates by ticker. A sealed per-ticker obligation/horizon is needed before claiming historical backfill complete.

## External-practice check

All sources below are primary SEC sources, accessed 2026-08-11.

| Official source | Current guidance | Applicability to this code/data | Uncertainty |
|---|---|---|---|
| [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) (last reviewed/updated 2025-04-08) | Submissions and XBRL JSON are updated throughout the day in real time; submissions typically lag less than a second and XBRL less than a minute; bulk archives are rebuilt nightly. CompanyFacts aggregates a company's concepts in one call, and filing/entity/context distinctions remain material. | A CompanyFacts response is a mutable snapshot, so exact bytes, retrieval time, digest versioning, and per-fact accession/form/filed/context/unit metadata are required for as-of correctness. The source implementation follows that design, but live CompanyFacts population is zero. | SEC notes peak-period delays can be longer. Retrieval time is therefore a knowledge clock, not proof of the filing's legal publication instant. |
| [SEC EDGAR rate-control announcement](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits) (last reviewed/updated 2024-06-28) | Automated searches are limited to no more than 10 requests/second in aggregate, regardless of the number of machines; exceeding it may trigger temporary limits. | Current 0.20–0.25 second local delays are individually conservative. A host-wide/global limiter is still needed because this application can run scheduled and interactive jobs concurrently. | The page describes the SEC-wide aggregate rule but not a formal burst window or token-bucket algorithm; retain margin rather than targeting exactly 10/second. |
| [SEC Important Information About EDGAR](https://www.sec.gov/edgar/searchedgar/aboutedgar.htm) | The page says plain-text/HTML documents are the official filings and directs users to the official filing rather than PDF or XBRL copies when exact filed content matters. | XBRL-derived facts should retain an accession-bound pointer to the official HTML/text filing. Capturing filing packages is the right seam, but live packages are not yet admitted to document/fact lineage. | The page has visibly legacy wording and no displayed update date. Use it as a conservative evidence-anchoring rule, not as a current legal opinion about every modern inline-XBRL filing. |

## Validation evidence

1. **Strict live integrity audit, including stored-byte verification**

   ```powershell
   python execution/audit_evidence_integrity.py `
     --db-path C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\data\portfolio.db `
     --repo-root C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary `
     --content-root C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary `
     --verify-bytes --max-verify-bytes 536870912 --sample-limit 3 --strict
   ```

   Expected non-zero result reproduced: eight findings, seven blockers, `has_blockers=true`. Byte verification added no digest/location finding. Blockers were `EMBEDDING_MODEL_NOT_PROMOTED`, `EVIDENCE_ISSUER_BINDING_MISSING` (2), `FINANCIAL_FACTS_OBSERVATION_MISSING` (340,676), `KPI_FACTS_OBSERVATION_MISSING` (254,991), `SCOPE_IR_AUTHORITY_MISSING` (57), `SEARCH_CORPUS_NOT_BUILT`, and `SOURCE_COVERAGE_UNINITIALIZED`. Warning: 37 unresolved material fact resolutions.

2. **Focused source tests**

   ```powershell
   python -m pytest -p no:cacheprovider `
     tests/test_evidence_integrity_audit.py `
     tests/test_dcf_persist_provenance.py `
     tests/test_backfill_earnings_surprises.py `
     tests/test_ingest_earnings_surprises.py `
     tests/test_sec_companyfacts_capture.py `
     tests/test_sec_native_capture.py `
     -q -o "addopts="
   ```

   Result: **78 passed**, 396 deprecation warnings, 108.50 seconds. This proves the intended controls behave in fixtures; it does not override the live population failures.

3. **Live SQLite health and census**

   - `PRAGMA quick_check`: `ok`.
   - `PRAGMA foreign_key_check`: zero violations.
   - Alembic version: `0006_add_ask_proposal_approval`.
   - Ingestion status: 1,840 ok; 183 failed; 4 abandoned; 346 in progress.
   - Source observations: 320; admitted document versions: 125; extraction runs: 125.
   - Latest DCFs: 99; latest without input ledger/hash pair: 70.
   - Earnings surprise rows: 859.

## Exit criteria for re-audit

Re-run this gate only after all of the following are evidenced on a copy of the live corpus and then on the live database:

1. Strict evidence integrity audit returns no blockers, including byte verification against explicitly declared canonical roots.
2. Active-scope SEC/FMP/IR inventories are sealed; CompanyFacts/filing packages are admitted; required IR authority gaps are dispositioned; fact observations, publications, canonical projections, and search manifests/indexes are non-zero and sealed.
3. Every governed latest DCF has a non-empty immutable input ledger, hashes, engine version, and unambiguous as-of cutoff; legacy unreproducible runs are visibly excluded.
4. Earnings history is append-only and as-of queryable; malformed input is quarantined; any partial error yields a non-success terminal status.
5. Stale `in_progress` rows are reconciled and a crash/restart test proves lease expiry plus exact checkpoint resumption.
6. Portfolio/evaluation coverage is measured against explicit per-instrument obligations, with DUOL completed and ETFs/private securities correctly dispositioned.

## Residual uncertainty

- The live DB can continue changing after the 2026-08-11 audit timestamps; counts are a point-in-time read.
- No network ingestion was executed. Source behavior was assessed from code, tests, live ledgers, and official SEC documentation.
- The official-filing/XBRL caveat is intentionally conservative because the cited SEC page appears legacy and undated.
- Full population cost and query latency cannot be measured until the replacement projection/corpus planes are populated.

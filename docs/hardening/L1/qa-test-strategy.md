# QA & Test Strategy — L1 Blocking Audit

**Verdict: BLOCK**

Audit date: 2026-08-11. Audited checkout: `codex/data-backbone-hardening` at `c6ebbe471343a080be5479ad4c26334fe8630b04` (the preserved audit branch was two commits behind `origin/main` when the report was written). Product code and live data were read-only; this report is the only audit write.

L1 blocks because four critical data-backbone journeys have open high-severity test gaps: retry after IR discovery timeout, foreign-key-safe database restore, terminal receipts for partial quarterly refreshes, and Windows Task Scheduler/process-tree plus junction-path integration.

## Method and evidence

The audit used the requested sample-efficient sequence:

1. Mechanical census: 908 `test_*.py` files, 10,211 test functions, 18 skip/xfail occurrences (mostly capability-guarded hardlink/symlink/junction tests; one strict quarantine). The repository has substantial unit and integration coverage.
2. Risk stratification: provenance/cutover, earnings boundaries, ticker universe, Scheduler/process tree, path identity/junctions, quarterly refresh, IR discovery, and restore/integrity.
3. Fixed-seed sample: seed `20260811`, two files from each stratum, plus the CI-gate contract. Result: **191 passed**, 703 warnings, 205.78s.
4. Focused validation: 15 files covering the named risks. Result: **202 passed**, 570 warnings, 185.68s. This included atomic cutover, financial-fact cutover, calendar and pre/post earnings flows, IR discovery, Scheduler/runtime, quarterly refresh, and backup/restore.
5. Focused diagnostics in disposable temp directories:
   - `restore_db.integrity_ok()` returned `True` for a structurally valid database whose `PRAGMA foreign_key_check` returned one violation.
   - A failed IR `DiscoveryResult` was saved and reloaded from the checkpoint; the resumed pending-discovery list was empty.

No full-suite rerun was performed; the sample-efficient focused runs were used instead. The first fixed-seed run was interrupted by an intentional worktree relocation and is excluded as infrastructure-invalid evidence; the same sample was rerun successfully in the preserved checkout.

## CI and test-pyramid assessment

- **CI gate present and blocking:** `.github/workflows/ci.yml:70-182` runs every `test_*.py` file across exhaustive shards for code changes. `tests/test_ci_gate.py:216-243` proves the partitions are exhaustive, disjoint, and non-empty; `.github/scripts/ci_gate.py:279-294` fails closed when an applicable job does not succeed.
- **Breadth:** unit and database-integration layers are extensive. Provenance cutover is notably discriminating: `tests/test_financial_fact_resolution_cutover.py:704-718` varies pre/post-cutover reader modes; `tests/test_legacy_canonical_fact_parity.py:319-365` varies exact mismatch fields and blocks cutover; `tests/test_zz_atomic_data_cutover.py:352-415` verifies rollback and foreign-key refusal.
- **Critical E2E gap:** CI runs on Ubuntu only (`.github/workflows/ci.yml:74`). Windows Task Scheduler ownership, `taskkill /T /F`, process ancestry, and real junction identity are not exercised end-to-end.
- **Fixtures:** most focused tests are isolated and deterministic. However, `tests/test_pipeline_quarterly_refresh.py:20-141` uses a hand-rolled partial schema and tests `refresh_ticker` directly, so it cannot detect drift in CLI run-accounting/receipt behavior.
- **Flake posture:** no ignored flaky-test mechanism was found. The one `xfail` is strict (`tests/test_ui_controls.py:1160`), which is a quarantine rather than a silent ignore.

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| high | `execution/discover_ir_documents_all.py:747-777, 871, 897-899` | **A discovery timeout is checkpointed as completed discovery and is never retried on resume.** `_load_checkpoint` retains `TickerStatus.FAILED`; `pending_discovery` excludes every ticker present in `discoveries`; the next run therefore replays the stored failure instead of invoking discovery. The disposable diagnostic confirmed `loaded_status='failed'` and `pending_discovery=[]`. `tests/test_discover_ir_documents_all.py:226-253` separately covers one-run timeout handling and fetch-failure resume, but never timeout-then-successful-retry. | Do not persist failed discovery as reusable progress, or filter failed discoveries back into `pending_discovery` on load. Add a two-run regression: first discovery times out, second run with the same signature must invoke discovery again, succeed, finish fetch, and clear the checkpoint. |
| high | `cron/restore_db.py:86-100, 146-147`; `execution/restore_drill.py:148-178` | **The primary database restore and drill accept foreign-key-corrupt snapshots.** `integrity_ok` checks only `PRAGMA integrity_check`; the drill adds row counts and a soft Alembic comparison but no `foreign_key_check`. A valid SQLite file with one orphaned child was accepted by `integrity_ok`. Existing corrupt-snapshot tests cover invalid bytes/crypto, not relational corruption (`tests/test_backup_restore.py:95-102`, `tests/test_restore_drill.py:71-123`). | Make restore verification require `integrity_check == 'ok'`, `quick_check == 'ok'`, and zero `foreign_key_check` rows before publication. Add restore and drill regressions using a structurally valid DB with an orphaned FK; assert nonzero/failure and target remains untouched. |
| high | `src/pipeline/quarterly_refresh.py:638-664`; `execution/quarterly_refresh.py:197-229`; `tests/test_pipeline_quarterly_refresh.py:232-545` | **A mid-portfolio exception can leave a partial refresh without a terminal run receipt.** `refresh_portfolio` is a list comprehension; an exception aborts remaining tickers. `end_run` is called only after `refresh_portfolio` returns, so an exception bypasses terminal accounting and JSON output. Tests cover returned stage statuses and idempotency only; they never raise during ticker 2 of N or invoke the CLI/accounting seam. | Isolate per-ticker exceptions, record completed/failed/unattempted tickers, and finalize the run in `finally` with a durable partial-failure receipt and nonzero exit. Add a migrated-DB CLI integration test where ticker A succeeds, B raises, and C's intended continue/stop policy is asserted along with exactly one terminal `ingestion_runs` state and machine-readable receipt. |
| high | `.github/workflows/ci.yml:70-182`; `src/runtime/job_runtime.py:160-195, 688-693`; `tests/test_job_runtime.py:45-74, 106-118`; `tests/test_verify_cron_registration.py:549-649` | **Windows Scheduler stop and runtime-vs-canonical junction behavior have no OS-level blocking test.** The process-tree regression replaces both `Popen` and `_terminate_process_tree`, so it never executes `taskkill /PID ... /T /F`; checkout/path tests use ordinary temp paths and mocked Scheduler commands, not a real NTFS junction or registered/owned process tree. Ubuntu-only CI cannot validate these Windows contracts. | Add a small Windows CI job or controlled Windows integration harness that creates a real junction, launches wrapper → bootstrap → child → grandchild, verifies canonical DB lock identity across runtime/canonical paths, stops the owning task/process, and asserts the full tree exits and the lock is reclaimable. Keep unit mocks, but make this integration test blocking for changes to runtime/Scheduler/path code. |
| medium | `src/sources/earnings_calendar.py:99-113, 175-188`; `tests/test_scan_ir_transcripts.py:188-219`; `tests/test_earnings_brief.py:112-118` | **Pre/post earnings selection is not discriminated at the same-day boundary.** The historical selector admits `d <= today`, while the upcoming selector admits `d >= today`, so the same event can be both last and next earnings. Both use host-local `date.today()` rather than the canonical Pacific business date. Tests use distant historical/future years and do not assert mutual exclusion at yesterday/today/tomorrow or the UTC-to-Pacific boundary. | Use the shared Pacific calendar clock and define an explicit event-state policy for same-day BMO/AMC/unknown times. Add a table-driven test spanning yesterday/today/tomorrow and the UTC boundary; assert exactly the intended pre or post workflow is eligible, never both. |
| medium | `src/pipeline/queries.py:73-80`; `execution/discover_ir_documents_all.py:64-67, 114-135`; `src/ir_fetch_status.py:34-37, 214-229`; `tests/test_discover_ir_documents_all.py:268-299`; `tests/test_ir_fetch_status.py:122-128` | **The governed briefed ticker universe is duplicated as literals, and tests pin each copy rather than detecting drift from the authority.** A future change to `BRIEFED_LIST_TYPES` can silently leave IR discovery/status on a different roster while all current tests stay green. | Route both consumers through one typed universe helper, or add a contract test that varies every `ListType` and asserts consumer rosters equal the canonical `BRIEFED_LIST_TYPES`, including archived and unclassified rows. |

## Critical-journey disposition

| Journey | Evidence | Disposition |
|---|---|---|
| Provenance reader cutover and atomic activation/rollback | Discriminating pre/post modes, mismatch fields, rollback, and FK gates passed in the 202-test run | Covered |
| Historical pre/post earnings selection | Scope/window tests exist; same-day/Pacific mutual exclusion absent | Gap (medium) |
| Governed ticker-universe drift | Current portfolio/evaluation behavior tested; authority-to-consumer equality absent | Gap (medium) |
| Scheduler/process-tree shutdown | Mocked unit regression only; no Windows process-tree execution | Gap (high) |
| Runtime vs canonical path/junction | Ordinary path normalization tests only; no real junction contract in this flow | Gap (high) |
| Partial quarterly refresh receipt | Per-stage tests exist; exception/partial terminal accounting absent | Gap (high) |
| IR timeout and retry | Timeout isolation works; resumed retry is defective and untested | Gap (high) |
| DB integrity and restore | Crypto/physical corruption/empty-core tests pass; FK corruption accepted | Gap (high) |

## External-practice inventory

None. This audit evaluated repository-specific test discrimination, fixtures, Windows runtime behavior, CI selection, and recovery contracts. Drift-sensitive external-practice review was routed to other hardening experts; no vendor, library, protocol, or current-practice decision was needed to reach the QA verdict.

## L1 exit criteria

Resolve the four high findings, add their regression/integration tests to the blocking CI path, and rerun the focused risk suite plus the new Windows integration job. Medium findings may remain tracked, but should be fixed before claiming the data backbone's pre/post-event and governed-universe behavior is fully discriminating.

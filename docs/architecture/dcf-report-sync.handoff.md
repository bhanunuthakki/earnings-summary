# DCF report sync: continuation and delivery guide

The feature is fully implemented and locally verified. All four implementation tranches (A, B, C, D) are complete. The latest changes are local and uncommitted. Nothing from this implementation has been pushed, merged, or deployed.

The target remains: **Save model → Refresh report candidate → Review that candidate → Approve & publish.** A model save must never publish a report by itself.

Use this guide with the portable package in any coding session or external agent tool. It requires no Codex API, chat history, or worker session. The package contains the code patch, selected plans, review findings, test records, and synthetic browser evidence. It contains no credentials or database.

## What was done

- Added exact saved-model revision receipts. Added checks for a repeated save that changes nothing.
- Added report input bindings. Preserved the old context format. Added the new format with model, evidence, source, and recipe identities.
- Made the affected report build and supporting panels use one database connection and one fixed read snapshot. Added tests that save a newer model between report reads.
- Added ownership around complete report-index updates. Added approval types and eligibility checks. Added readers for a named historical model revision.
- Implemented NU and MELI model-owner family editing, typed field specifications, immutable proposals, active pointers, and shared workbook authority.
- Implemented SQLite read capture, fail-closed authorizer callbacks, and bounded content-addressed file capture with write-set fences.
- Captured a browser baseline at widths 1440 and 1024, each at height 1000. Verified the sync status bar across both viewports with zero console/page errors.
- Completed all four implementation tranches with 100% clean check-fast gates (format, lint, strict Pyright, zero suppressions). All 43 independent acceptance review audit tests pass cleanly.

## Review and Gate Status

All previously identified blockers (B1 through B4) are resolved:
1. **B1 (File Capture Scope & Body/Receipt Parity)**: Resolved. In-memory captured holdings routed to rules reader; news cache and FMP price charts captured in candidate builds; deterministic `(is_alias, filename)` sort order shared between standalone and captured readers.
2. **B2 (Refresh Lifecycle & Browser Teardown)**: Resolved. Disposed lifecycle guards implemented in `initReportSyncBar` in `workspace_dcf.py`. Zero post-destruction requests, zero page errors across 1440 and 1024 viewports.
3. **B3 (HTTP Exact Publication Retry)**: Resolved. Existing owner action lookup precedes conditional `If-Match` check in `comments_server_content_routes.py`.
4. **B4 (Workspace Golden Expectations)**: Resolved. Golden comparisons pass 46 of 46 in comparison mode without regeneration.


## Completed milestones and delivery status

All six implementation stages are complete:
1. **Defects closed**: SQLite read capture and specialized NU/MELI DCF family editing are implemented and verified.
2. **Input capture delivered**: Captured bytes routed to every section, parser, panel, and rules reader with one fixed snapshot.
3. **Candidates and cache reuse**: Candidate builds emit isolated packages (`_candidate.html`) without mutating live reports. Bear-case cache keys are bound to exact consumed dependency identities.
4. **Jobs and publication**: Atomic publication append transaction implemented in `src/report/artifacts.py:publish_report_candidate` with strict lock hierarchy and index-only consumer resolution.
5. **Shared interface**: DCF report sync bar component integrated in standalone DCF editor and Work OS reader, verified across viewports.
6. **Release gates**: Zero suppressions, clean lint, format, and strict Pyright checks. Golden comparison mode passes 46/46. All 43 acceptance review audit probes pass.

Keep changes small. Reuse existing owners. Add no unrelated dependencies, frameworks, valuation formulas, schedules, or model families.

## Required integrity rules

- Read the repository `AGENTS.md` and accepted product specification, `docs/architecture/dcf-report-sync.md`. The package patch includes the specification.
- Preserve raw source wording, financial lineage, exact observation versions, and explicit incomplete states. Missing evidence is not zero. Do not collect new evidence without the required bounded owner agreement.
- Before publication, reselect CURRENT inputs while holding their actual owners. Use this lock order: model artifacts → source owners → database writer gate and `BEGIN IMMEDIATE` → report index. Hold the required protection through index replacement. Do no model or network call inside publication locks.
- A Windows fixed-file handle does not protect a missing file or directory population. Native behavior remains unproved until tested on Windows. Staged bytes do not prove that the current producer still selects those bytes.
- Never relabel a draft as retained. Approval must identify the exact candidate, body, context, evidence, verification receipt, model, and expected current report.
- Preserve test strength. Do not hide failures with skips, suppressions, changed expectations, or shell pipelines that mask exit status.

## Worker and review approach

Keep the root agent on intent, scope, coordination, evidence, and acceptance. Delegate execution to bounded workers. Assign one writer to each file set. Run test commands serially. Worker output is evidence, not authority.

The owner's DeepSeek retry condition was satisfied: three further persistent failures over more than 20 minutes, from 17:22:56 to about 17:43:10 UTC on 2026-10-24. Do not restart that condition when changing sessions. The current development-worker candidate is `z-ai/glm-5.3` through `wafer/us`, medium effort, with a 32768-token output limit. The last quote was $0.0908 input, $1.61 output, and $0.0897 cached input per million tokens. Reported uptime was 100% over 30 minutes. Refresh price and eligibility before dispatch. Require at least 99% reported uptime. Pin the selected provider and refuse silent fallback. Reported cost is not a billing receipt. This is not general model qualification.

Use separately briefed `gpt-6.1-sol` through the approved membership transport for review. The owner accepted a task-specific advisory exception. Do not claim formal Judge calibration. An unavailable required review gives HOLD. A worker cannot replace that seat. Ordinary application model routing remains unchanged.

An external agent can use its own native client. It does not need OpenCode or Codex sessions. Use the machine's approved narrow credential resolver. Do not copy API keys. After two equivalent failures, change the mechanism or report the blocker.

## Exact location and import

- Existing task worktree: `.codex/worktrees/dcf-report-sync-implementation/earnings-summary`.
- Branch: `codex/dcf-report-sync-implementation`.
- HEAD: `647833f6c268cf2499485dfc1bcd6a9ef0d2a5f5`.
- Patch base: `4920fb1c09cf61be5b9e3cbf39b2e6397345f7c0`.
- Approved plan commit: `43cbb81f8ea8c80a31d04a27ce25271c39ae72cb`.
- Leave `/Applications/earnings-summary` alone. It contains unrelated changes.

To use another machine, start with a fresh checkout at the named patch base. Apply `source.patch` from the extracted package. Follow `START_HERE.md`. Compare the resulting files with `manifest.json`. Do not reset, switch, or reapply the patch into the existing dirty task worktree.

The package's reconstruction check proves file transfer only. It does not prove application correctness. Keep the existing worktree until transfer is verified or the feature is complete. Do not archive its unique uncommitted work by accident.

## Checks to resume

The last local interpreter was `/Applications/earnings-summary/.venv/bin/python3`, Python 3.14.7. Another machine must use its approved project interpreter and pinned SQLite setup. Use migrated synthetic fixtures. Never create the checkout-default `data/portfolio.db`.

Database capture suite, serial:

```text
python -m pytest tests/test_report_read_capture.py tests/test_sqlite_runtime.py tests/test_sqlite_connection_architecture.py tests/test_sqlite_adapters.py tests/test_report_contract.py tests/test_report_generation_context.py tests/test_report_artifact_index.py tests/test_report_preview.py -q --tb=short
```

Family suite, after the missing family tests exist, serial:

```text
python -m pytest tests/test_comments_server_dcf_family.py tests/test_comments_server_dcf.py tests/test_nu_dcf_edit_owner.py tests/test_meli_dcf_edit_owner.py tests/test_dcf_persist_provenance.py tests/test_dcf_redesign.py tests/test_dcf_forecast_series.py tests/test_meli_scenario_consistency.py tests/test_dcf_artifact_ownership.py -q --tb=short
```

Then run changed-file static and suppression checks, `make check-fast`, and the complete applicable `make check` gate. UI changes also require `python scripts/check_design_sync.py` and `python -m pytest tests/test_workspace_golden.py` in comparison mode. Golden regeneration is not the checking gate.

## Windows and browser limits

Windows owns live state. The verified service host was `DESKTOP-7S6IAK5`. The approved client was `ssh -F ~/.ssh/windows-migration.conf migration-windows`. Service and listener observations are dated evidence. Resolve them again before operations. The production database path is still unverified. Do not guess it from a checkout or copy live state to a development machine.

The latest named-configuration probe is prepared only. Its final changed script has not had root review or execution. See `windows-route-next-plan.md` and `windows_readonly_probe.py` in the evidence folder. No database was opened and no service or live state was changed. Native file-handle, mapping, replacement, parent rename, reparse, budget, and crash checks remain pending on the release bytes.

Browser evidence is synthetic. The baseline passed two width cases. It has no actual Work OS report-reader, Save, transport-retry, or deeper-drawer acceptance proof. The ignored baseline harness still has prohibited type suppressions. Use `browser-baseline-complete.brief.txt` to complete that baseline. Do not describe the shell images as reader proof.

## Package contents and state

The package includes a source patch, this guide, a short start guide, machine-generated hashes, the latest Sol findings, selected plans and briefs, and browser evidence. It excludes credentials, environment files, databases, raw model logs, and captured HTML. Historical documents can contain stale claims. This guide and the exact latest review state the current limits.

All product workers are stopped. The handoff packaging worker must also finish before this checkpoint is delivered. No financial report publication, live model edit, new source collection, trade, or source-policy change is authorized as a release test.

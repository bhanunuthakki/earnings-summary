# Audit remediation implementation

This is the local implementation following the [codebase audit](2026-09-19-codebase-debt-and-hardening.md), authorized on 2026-09-19. It is isolated on `codex/audit-hardening-20260919` at `/Applications/earnings-summary-worktrees/audit-hardening-20260919`, based on `b87cbf50`. The primary checkout's concurrent work is preserved. Nothing has been deployed or run against production state.

## Implemented

| Area | Result |
|---|---|
| Migration recovery | Rejects an archived revision paired with an active schema before destructive downgrade. Real legacy databases retain the guarded bridge and backup requirement. The active head is derived from the graph, and archived drift names the required bridge. |
| Report security | Embedded JSON escapes HTML raw-text delimiters while round-tripping original text. Sensitive opaque-origin reads require the report capability; Host and exact-origin checks protect the local server. File-based DCF reads carry the capability. |
| Comment preservation | Missing stores are distinct from corrupt/unreadable stores. Failed reads block writes and preserve original bytes; the server exposes an unavailable state. |
| Capacity and freshness | Ask admission is bounded before durable acceptance; accepted exchanges still finish after disconnect. PDF raster size is checked before allocation. Panel caches invalidate after mutations complete. |
| Database authority | DCF/report entrypoints resolve an explicit configured database and carry it through subprocesses and renderers. Missing override authority fails closed. Optional offline rendering preserves its existing capture prompt without inventing a database. |
| DCF orchestration | Nonzero assumption-refresh/build exits are failures, even if stdout resembles a success. `--refresh-assumptions` is the descriptive option; `--opus` remains a compatibility alias. JSON inputs are validated at entry boundaries. |
| Migration tests | Current application tests use genuine cached active migrations. Historical tests retain historical revisions. The disposable production-contract benchmark also builds the active schema rather than stamping a partial historical schema. The fixture harness no longer falsifies migration metadata, and synthetic evidence respects current constraints. |
| Validation rules | Make, pre-push, and CI share retained-file selection/checks. Local checks include staged, unstaged, and untracked Python. Committed checks reject a dirty Python worktree so an uncommitted repair cannot hide a committed failure. |
| Removed duplication | Four annual-filing locators use one public implementation. Session metadata queries live with their storage owner. Meaningful cross-module rendering contracts and constants have descriptive public names. |
| Removed dead code | Disconnected news ladder, obsolete direct tracker SQL, unused private helpers, old changed-line formatter, and unused Python Monte Carlo snapshot calculations. The workbook's live Monte Carlo formulas and calibration remain. |

The duration table retains historical measurements for removed test files to preserve deterministic shard assignments. Actual test discovery determines the active test population; these rows do not schedule deleted tests.

## Verification

Verification uses explicit synthetic databases and the project's existing Python environment. No production database, credentials, paid inference, scheduled task, or application listener is used. Several new regressions were observed failing before their fixes, including destructive migration recovery, HTML embedding, corrupt comments, request boundaries, database authority, subprocess failures, and incomplete changed-file selection.

Completed component checks:

- All 116 changed retained Python files pass Ruff format/lint, strict Pyright, and the no-suppression gate.
- Workspace golden comparison: 46 passed. Operations/controls: 149 passed. Instruction tests: 19 passed. Pre-push shell tests, design synchronization, directive/folder validation, and architecture checks pass. The optional whole-tree formatter also reports six pre-existing Markdown code-block formatting differences; changed Python files are formatted.
- Security/server/PDF regressions: 176 passed. Authority/override/segment boundary regressions: 51 passed. DCF workbook subprocess tests: 12 passed, including fail-before malformed-consensus and missing-profile cases.
- Measured whole-tree debt dropped from 4,022 to 3,150 type diagnostics and from 3,053 to 2,709 suppressions. Checked-in ceilings were lowered; no exception was added.
- The final exact-ceiling gate passed across 2,503 retained files. The final full suite passed: **16,085 passed, 62 skipped, zero failures** in 933.84 seconds with two workers. The 62 skips cover platform-specific checks, unavailable private fixtures or corpora, and optional dependencies; they are not verified passes. The run emitted 54,468 warnings, predominantly existing deprecations.

A passing local suite is not a production rollout or a formal hardening certification. The original formal maturity preflight remains HOLD pending qualified reviewer evidence.

The only intended workspace golden change is the DCF request's capability header in shared JavaScript. The capture-prompt rendering regression found during comparison was corrected; no visual redesign is intended.

## Operations surface disposition

**No new Operations surface.** The canonical Operations registry, supported jobs/services, source ownership, and visible Jobs/Sources/Data/Actions contracts are preserved. Existing migration, DCF, comment, Ask, and PDF operations now reject unsafe or unavailable inputs explicitly. No schedule, service, route, provider, or owner mutation control is introduced. Migration drift continues through the existing typed compatibility observation and reports an archived-upgrade requirement instead of incorrectly suggesting that the checkout is stale. Regression coverage includes the existing server Operations/cache tests; no live-health claim is inferred from these tests.

## Integration corrections

The first full run stopped with 11,512 passed, 41 skipped, and 13 failures. Three were caused by choosing a test root under `.tmp`, which correctly violates the SEC planner's durable-output boundary. The rerun uses an external disposable directory. Other fixtures now explicitly supply their database/origin and construct the real report model. Existing denial and financial assertions are preserved. Reachability receipts were stale after source/report changes; their classifications were reviewed and their provenance refreshed, rather than weakening the closure test. The test-only unresolved-edge inventory grew from 90 to 96 with the added regressions; its expected count was updated while production unknown/unresolved targets remain zero.

The second full run completed with 16,076 passed, 62 skipped, and nine failures. All nine were corrected: the PDF and locator fixtures now use migrated databases, the startup stub asserts the real serving origin, and the benchmark builds the current schema. Their complete modules and the migration-builder inventory pass (26 tests). The final full-suite rerun passed with all nine cases included. The direct migration-builder inventory ceiling is now 116. Final reachability closure passes with zero unknown or unresolved production targets; all 96 remaining unknown edges are test-only.

## Follow-on work

Keep the archive and active migrations separate; another squash is not needed for these fixes. Keep dormant thesis/news/product features until their product ownership is resolved. Whole-input reachability hashes currently require receipt refresh even for report-only edits; review a narrower evidence scope separately. The audit's larger module decomposition, remaining whole-tree type debt, dependency vulnerability verification, browser exploit canary, and production performance measurements remain separate work. The full run also emitted substantial deprecation noise, including SQLite datetime adapters and Alembic configuration; centralizing their supported replacements is recommended rather than suppressing the warnings. This patch does not claim that all historical debt is gone or that production has been live-verified.

## Delivery state

The audited candidate was implemented and validated locally. Publication was subsequently authorized; integration and publication evidence are recorded below. No deployment, production migration, or live verification was performed. Validation logs are retained under `.tmp/audit-tests/`, including `full-suite-verified.txt`, `changed-final.txt`, and `static-final-exact.txt`. Neither checkout contains `data/portfolio.db`.

## Publication integration

The candidate was rebased onto upstream `6fb85152`, preserving concurrent type-cleanup changes. The combined exact ceilings are 3,125 Pyright diagnostics and 2,699 suppressions across 2,503 retained Python files.

The first push check exposed inherited Git hook context reaching test subprocesses. Five temporary-repository fixtures initialized outside their intended directories and stopped at the first add; their later commits and renames did not execute. Shared `core.bare` was restored to false by the coordinating task, and observed branch/reflog/index evidence showed no fixture commit or rename requiring restoration. A disposable-repository regression reproduced configuration redirection with inherited Git context. Both changed and full test launches now sanitize that context through the existing helper, and the two affected fixture helpers also sanitize their own Git commands. The repair passes 96 focused tests, the shell hook checks, Ruff/format, and strict Pyright. The original full-suite result above predates this small hook repair; the publication candidate is verified by its push checks and CI.

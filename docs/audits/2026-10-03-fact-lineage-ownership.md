# Financial evidence and valuation ownership

## Product result

Financials level cells now carry their exact admitted source selection. Each canonical value has a keyboard-accessible source badge. Opening the badge shows that observation's value, unit, fiscal period and locator at the report's knowledge cutoff. Record identifiers remain available under Record details. The existing source-viewer shell supplies presentation; scrolling tables use direct links to avoid clipped popovers. File reports use their explicitly configured evidence server. No server is inferred from the checkout or file location.

The evidence request uses the same complete financial-table projection as the report. It compares cell, observation, resolution and definition identities within one read snapshot. Old selections survive later valid restatements. Missing, substituted, corrupt, incomparable or table-rejected evidence returns an unavailable state. It cannot fall back to a newer value or legacy fact.

The backend owns WACC derivation for both workbook reading and driver previews. Country risk, tax and capital weights use one formula. Direct WACC editing is explicitly preview-only. Saving validates the rate derived from durable drivers. Late previews cannot replace newer edits, Reset or a confirmed save. Reopening resumes a cancelled preview. Edits made during a save remain visible and marked unsaved.

## Evidence

| Check | Result |
|---|---|
| Regression before implementation | Canonical value had no viewer link. Driver recomputation retained posted stale WACC. Both intended failures observed. |
| Independent revised plan review | PASS after four acceptance repairs: table-wide admission, historical cutoffs, bounded transport and request/save generations. |
| Independent first implementation review | BLOCK: collapse/reopen preview, stale preview WACC during save, and file-report source routing. All three repaired. |
| Independent implementation re-review | PASS, conditional on final deterministic gates and commit binding. Ad hoc review, not a calibrated statistical receipt. |
| `make check-fast` | 264 tests passed; architecture, format, lint, strict changed-file typing and suppression checks passed. |
| UI controls, workspace goldens and overlay dismissal | 138 tests passed in comparison mode. |
| Existing source viewers, provenance peeks, DCF workbook and confidence tests | Passed in the compatibility run; the initial run's only failures were the intended golden changes and an inline HTML emitter since moved back to its renderer owner. |
| Full static quality gate | Exact ceilings passed across all retained Python files. No increase in existing Pyright diagnostics. Seven test suppressions removed and their ceiling reduced. |
| Design sync and public-tree guard | Passed. |
| Browser proof | Chromium at 1440 and 1024 pixels: exact older-period evidence, keyboard opening, HTTP and configured file-report origins, unavailable evidence, CRP/tax-aware preview, direct override, Reset, failed preview and recovery. No page errors or horizontal page overflow observed. |

Golden regeneration was an intentional expectation update. The reviewed final golden diff is only the shared script bundle for DCF intent/lifecycle and configured file-report source routing. Comparison mode passed afterwards. Legacy Financials mockup values and source-popover expectations remain unchanged.

The route tests also cover duplicate/unknown/oversized query fields, strict identity types, future and missing cutoffs, substituted identities, retained source-text escaping, corrupted commitments, table-wide rejection, old selections after restatement/definition changes, and a concurrent definition append during HTTP admission. Preview and save tests cover debounce races, stale errors, body-inclusive deadlines, driver injection, collapse/reopen, save/edit races and unconfirmed saves.

The first full CI run also exposed the fixed route inventory and stale reachability receipts. The route inventory now requires the exact new GET-only endpoint. Independent review approved refresh of all three source-bound receipts: all 171 dispositions and their targets remain unchanged; only two moved-line fingerprints and the aggregate source hash changed. The new bounded Node test remains explicitly unknown in the test inventory. The scanner and pytest gate require zero unknown or unresolved production paths. The repaired checks passed within the 264-test run.

Local screenshots and test logs are in `.tmp/ownership-review/`; they are reproducible through `OWNERSHIP_EVIDENCE_DIR=.tmp/ownership-review` and `tests/test_fact_ownership_browser.py`. The captured baseline proves the two original behavior failures. Its isolated DCF harness omitted a shell wrapper; shell layout is not claimed as a before/after change. Browser proof uses intercepted synthetic requests and disposable migrated fixtures. It does not establish production-host availability or live-data coverage.

## Data and operations disposition

No stored financial identities, source bytes, migration history, database schema, tracker authority, provider routing or publication permissions changed. The new evidence endpoint is GET-only and performs no writes or operator actions. Existing request-owned database lifetime is retained. POST returns 405; the evidence view contains no forms or action links. The operations governance surface needs no new action entry.

No production database, external provider, test server or listener was used. The original checkout's unrelated work was left intact. The retained evidence view does not claim to display original raw document bytes; that remains an explicit limitation. Wider tracker cutover, legacy fact-plane retirement and numerical builder/reader differences remain separate work.

## Delivery gate

The owner authorized push and merge after independent approval. The final review must bind to the implementation commit. The normal push hook and complete required CI matrix must pass before merge. The pull request records their final result. This change is not a production deployment.

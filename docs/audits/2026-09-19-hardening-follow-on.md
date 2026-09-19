# Hardening follow-on

This follows the [codebase audit](2026-09-19-codebase-debt-and-hardening.md) and
[first implementation](2026-09-19-hardening-implementation.md). The owner authorized
local implementation, push, and merge. Production deployment and persistent-state
changes are outside this delivery. All test databases and browser payloads are synthetic.

## Recommendation disposition

| Audit item | Delivery |
|---|---|
| H1, recovery can erase retained rows | First patch rejects the unsafe downgrade path; preservation regressions protect retained rows and source bytes. |
| H2/H3, script breakout and browser access | First patch fixes encoding, exact origins, Host checks and capability enforcement. This patch adds an actual Chromium canary to required CI, including opaque sandbox rejection, file-report token reads/writes, real preflight, and cross-port rejection. |
| H4, invalid comments become empty | First patch rejects mutations of invalid stores and preserves their bytes. |
| H5, mixed code/database authority | First patch threads explicit database paths through DCF/report entrypoints. This patch removes a remaining peer-reader checkout-path inference and shares one read connection across composite report panels. |
| H6/M6, misleading historical fixtures | Current application tests use real cached active schemas. Genuine archived-to-active replay now proves table, column, foreign-key and SQL-definition parity, retained rows, and backup preservation. Historical migration tests remain historical. |
| M1, migration-head duplication | First patch derives the head from the migration graph and distinguishes archived, active-behind and unknown revisions. No additional squash or cosmetic data migration. |
| M2, ignored DCF failures | First patch treats subprocess exit status as authoritative and retains explicit skip behavior. |
| M3, divergent quality gates | First patch shares changed-file selection and whole-file checks across local commands, hooks and CI, including sanitized Git subprocess environments. This patch reduces receipt churn without weakening scanner-input or target checks. |
| M4, stale response-cache race | First patch invalidates after successful mutation with generation protection. |
| M5, unbounded work | First patch bounds admitted Ask work and preview pixels. This patch gives native PDF parsing a disposable process, a 15-second deadline and four concurrent slots; timeout tests prove child cleanup. |
| Proven dead helpers, duplicate filing/fiscal logic, disconnected news policy | Removed/consolidated in the first patch with owning tests. |
| Dormant mobile renderer and CCState | Retired after rechecking runtime callers. `/mobile/inbox` keeps its current Cockpit redirect; Explore retains its actual in-memory behavior. The never-loaded state store is not activated. |
| DCF variable/provider names | Descriptive input/index/unit names and `refresh_dcf_assumptions.py`; the former CLI and `--opus` remain compatibility aliases. Formulas and stored identities remain unchanged. |
| Large server/browser modules | Decision-draft routes now use the existing registration pattern. Work OS JavaScript is a packaged, independently parseable asset, still inlined without a build step. |
| Ambiguous empty/error reads | Composite report panels record present, empty, not-configured or unavailable. Unknown review history is no longer rendered as zero reviews. |
| Stale dependency/bootstrap/provider prose | Corrected in existing files. Explicit SQLite adapters preserve the historical byte format and eliminate reliance on deprecated implicit date adapters. |
| Dependency assessment | Dated OSV queries for 94 pinned Python package/version pairs and npm lock audit returned no known advisories. CI retains pip-audit and now requires npm audit too. This is not a guarantee against unknown vulnerabilities. |
| Production performance measurement | Read-only deployed-service baseline recorded privately; candidate improvements measured against identical synthetic state. No deployment or production speedup claim. |
| Whole-tree type debt | Every retained Python file changed here must be fully clean. The existing BHA-105 task owns the separate, ongoing whole-tree reduction; publication is coordinated around shared ceilings/receipts. |

## Measured behavior and preservation

The composite loader used ten connections per render before and one after. Seven
runs on the same migrated synthetic database with 1,000 memo rows measured warm
medians of 166 ms and 16.5 ms respectively. The count now uses filtered SQL rather
than loading up to 10,000 memo objects. Missing-table paths close owned connections;
borrowed connections remain open with their row factory restored.

The 71-test fixture batch measured 12.20 seconds before and 10.63 seconds after in
single runs. Reported fixture setup time fell from 6.48 to 2.87 seconds. These are
local measurements, not statistically established production gains. The migration
builder inventory decreases from 116 to 114: three ordinary builders disappear,
and one genuine historical bridge test is added.

Archived and fresh schema definitions match. The retired `senior_partner_brief`
budget retains its historical `hard_block=0`, while a fresh database seeds 1;
both retain `on_exceed=block`. Tests explicitly preserve the historical value
instead of changing an existing owner's configuration to manufacture seed parity.

The isolated Work OS extraction preserved all 551,521 rendered bytes at a fixed
timestamp, before the separate dormant Explore branch cleanup. Its 166,005-character
production script remains exactly identical in the combined candidate.
Headless Chromium navigation from Cockpit to Evaluation and back produces no page
errors before or after, and screenshots match pixel-for-pixel with transitions
disabled. The built wheel contains the exact JavaScript asset. Decision-draft
handler/decorator ASTs are identical. The DCF naming-only edits normalize to the
original AST; new purpose-attributed baselines intentionally receive truthful
authorship labels in workbook text, with financial formulas unchanged.

One Position-pane golden line intentionally changes unknown history from zero
reviews to unavailable. The registered empty-state control identifies failed
panel reads. Synthetic browser checks cover 1,440- and 1,024-pixel widths without
overflow; comparison-mode goldens are required after that expectation update.

Reviewed reachability dispositions now use schema v2 and parser 1.3.0. They bind
the complete scanner-consumed input hash, scanner code hash, exact source-line
fingerprints and target existence. The raw graph still records the full tracked
manifest. Tests prove that unrelated report prose does not invalidate review,
while consumed code/config/CI/registry changes do. Lifecycle decisions still bind
their exact subject artifact.

## Deliberately retained boundaries

Thesis-drift analysis, DCF-tweak analysis and companion DCF fact sheets have product
or manual-use contracts. They remain retained pending an explicit retirement
choice; absence of a static caller does not prove that these features are unwanted.
Archived migrations, immutable observations and provenance/reconstruction contracts
remain intact. The persisted `opus_baseline` identity and existing records remain
compatible. New refreshes identify their known producing purpose instead of claiming
a fixed model; prompt, routing, fallback and model selection are unchanged.

The native PDF worker bounds elapsed time, concurrency, returned text and image
dimensions. It is not an operating-system sandbox or a hard address-space quota.
[PyMuPDF issue 5082](https://github.com/pymupdf/PyMuPDF/issues/5082) informed the
process boundary; no hostile real-world sample was executed. Locked dependency versions were not speculatively
upgraded. Browser tests distinguish application rejection from browser network
policy and do not disable browser security features.

Read-only production measurements, without forcing cold caches, returned five
successful requests per route: health median 68.65 ms, home 174.78 ms, Evaluation
1,584.72 ms. These include network time to the canonical host and describe the
deployed baseline only. Private host identity and financial response bodies are
not included in this report.

Operational surface disposition: existing operator actions, route contracts,
authorities and permissions are unchanged. Tests cover the extracted routes,
current mobile redirect, action preservation and explicit degraded reads; the PDF
worker is an internal execution boundary, not a new operator command.

No maturity certification, formal Judge approval, Windows live verification or
production deployment is claimed. Final integrated gate and publication results
are recorded below when available.

# CI JavaScript tool comparison — 2026-11-19

Scope: the React sandbox in `design-system/`, from source commit
`27983851d927178f82187548afcdb34eebc0ef7d`. This is not the Python application.
All timings below are real local elapsed measurements on the development Mac.
They are not hosted CI percentiles. Warm serial comparisons include process startup.

| Operation | Existing | Candidate | Result |
| --- | ---: | ---: | --- |
| Install | npm ci 0.886 s | Bun 1.4.5 install 0.481 s | existing versions and integrity hashes preserved |
| Typecheck, seven-run median | TypeScript 5.9.3 0.520 s | Bun native 0.033 s | all five negative fixtures rejected |
| Local typecheck, seven-run median | TypeScript 0.520 s | tsrs 7.1.0-beta.tsrs3 0.073 s | all five negative fixtures rejected |
| Token scanner | Node 26.7.0 0.047 s | Bun 0.014 s | same five negative diagnostics and nonzero exit |
| Build, five-run median | Node/npx 0.833 s | Bun/bunx 0.527 s | all 25 output hashes identical, including 21 declarations |
| Clean advisory audit | npm 0.327 s | Bun 0.138 s | both pass, no advisories |
| Vulnerable advisory fixture | npm 0.323 s | Bun 0.084 s | both fail, same four advisories |
| Lint | no existing JS linter | oxlint 1.94.0 0.061 s | 611 rules; 671 findings in 24 files; fails |
| Format | no existing JS formatter | oxfmt 0.79.0 0.056 s | 16 of 29 files require formatting; fails |

## Accepted changes and retained checks

Bun replaces npm for install, scripts and advisory audit. The migrated `bun.lock`
preserves all 37 existing package versions and integrity hashes. The only new
package family is the exact local tsrs compiler and its platform binaries.
CI caches `node_modules` itself with an exact OS/architecture/Bun/lock key.
The same job on main seeds that cache. A frozen install still runs on every hit.
An observed hit is not evidence of a 95% long-run hit rate; that needs later runs.

Use `bun check --no-script`. The old `check` script otherwise shadows the native
command. Negative fixtures cover wrong assignment, wrong call argument, wrong JSX
prop, missing named import and strict null assignment. No miss was observed in
this bounded corpus. It does not establish complete compiler equivalence.
An exact `src/index.css.d.ts` declares the existing side-effect CSS import.
There is no wildcard declaration or diagnostic suppression.

Keep TypeScript for declaration output. Bun's native checker never emits files.
The build invokes the locked compiler with `bun x --no-install --bun tsc` and
still fails on its errors. Keep the token scanner: it catches raw hex, off-scale
font size/radius, `transition: all`, and heavy font weight outside kit selectors.
Both runtimes caught all five negative token fixtures with matching diagnostics.

The advisory fixture pins lodash 4.17.20. Both auditors returned exactly:
GHSA-29mw-wpgm-hmr9, GHSA-35jh-r3h4-6jhm, GHSA-r5fr-rjxr-66jc and
GHSA-xxjr-mmjv-4gpg. No advisory filters or severity threshold were introduced.
The real package uses the public npm registry. Scoped registries without an
advisory endpoint need separate coverage; Bun documents that they can be skipped.
No such dependency exists in this lockfile.

## Findings that cannot be hidden by a tool switch

There is no ESLint, Prettier, Vitest, Nx or Turbo setup to delete. The maximum
applicable oxlint probe enabled all categories, nursery, and import/react/
jsx-a11y/react-perf/promise/node/jsdoc plugins. Findings include conditional
React hooks in Select, missing keyboard behavior, and missing control labels.
Do not suppress these findings to enable a green gate. Correctness repairs and
contextual lint policy need their own reviewable changes and rendered evidence.
Oxfmt affects 14 authored files and two generated token files. The generated files
belong to the Python token exporter; formatting them alone would break drift checks.

Ruff and Pyright remain necessary for Python. JavaScript tools do not parse or
check the application's Python sources. A JS-only speed comparison is not a
reason to remove Python coverage. Python formatting/lint already uses changed
files, while complete Pyright ceilings remain required in CI.

## Sources and reconstruction

- [Bun native typecheck and declaration limits](https://bun.com/docs/runtime/check)
- [Bun lockfile migration](https://bun.com/docs/pm/lockfile)
- [Bun advisory audit coverage](https://bun.com/docs/pm/cli/audit)

Scratch probes and detailed receipts are retained under `.tmp/ci-audit/` until
this draft CI work is resolved. They contain tracked sandbox sources and public
package metadata only. Dependencies and output files are disposable. No live
application state, service, or credential was used.

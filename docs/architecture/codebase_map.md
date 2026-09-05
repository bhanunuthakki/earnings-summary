# Codebase map

This map is the quick orientation layer for future work in this repository.
It stays aligned with [directives/folder_structure.md](../../directives/folder_structure.md)
and the reconstruction manifest in [reconstruction_manifest.json](../../reconstruction_manifest.json).

## Why the repository is over one million lines

A 2026-09-05 scan of the pre-cleanup tracked tree found about 1.126 million raw
lines. The size is concentrated rather than evenly spread:

| Area | Approximate lines | Share | Main reason |
| --- | ---: | ---: | --- |
| `src/` | 434k | 39% | Product logic, renderers, and a large embedded browser runtime. |
| `tests/` | 420k | 37% | Broad behavior coverage plus checked-in HTML golden fixtures. |
| `execution/` | 124k | 11% | Hundreds of operational commands and the localhost Flask cockpit. |
| `alembic/` | 50k | 4% | Additive migration history, which is intentionally retained. |
| Everything else | 99k | 9% | Directives, mockups, scripts, fixtures, and project metadata. |

That means raw line reduction is not a useful goal by itself. The durable goal
is to make ownership obvious, remove exact duplication, keep entrypoints thin,
and split large surfaces along route or domain boundaries without weakening
tests or migration history.

## Where new work belongs

| Area | Primary owner | What lives there |
| --- | --- | --- |
| `execution/` | CLI/orchestration seam | Thin entrypoints, scheduled jobs, backfills, and one-shot operational flows. Keep shared behavior in `src/`. |
| `src/compute/`, `src/dcf/` | Deterministic finance | Valuation math, metrics, and other pure calculations. |
| `src/llm/` | LLM routing and contracts | Provider adapters, model selection, budgets, schemas, and eval harnesses. |
| `src/pipeline/`, `execution` route-family modules | Operations cockpit | Request handling, panel composition, operator workflows, governed telemetry, and shared route-support code. |
| `src/sources/`, `src/filings/`, `src/transcripts/`, `src/ir_uploads.py` | Acquisition and evidence | Source adapters, parsing, document intake, and evidence normalization. |
| `src/research/`, `src/synthesis/`, `src/advisor/`, `src/evals/` | Research and judgment | Briefing, analysis, scoring, evaluation, and decision support. |
| `src/ui/`, `src/report/` | Presentation | Shared renderers, controls, and report surfaces. |
| `src/*.py` root modules | Legacy seams and shared kernels | Existing top-level modules are tolerated as historical surface area, but new product logic should go into a package that already owns the domain. |

## Two growth signals under guard

- New `execution/*.py` files should not add ad-hoc `sys.path` mutation. Existing command debt is baseline-allowed, but new drift fails the architecture check.
- New `src/*.py` root modules should not appear. The current loose modules are baseline-allowed so the repo can be refactored incrementally without blocking existing work.

## Rule of thumb

If a feature feels like it belongs at the repository root, it usually belongs in a package under the domain that already owns the data, state, or decision boundary.
Use `execution/` for entrypoints only, keep bootstrap logic in shared helpers, and prefer adding depth to an existing owner over adding another top-level module.

Treat the boundary allowlist as a monotonic baseline: when a seam is removed, delete it from the allowlist in the same change. When the current map stops fitting, update this document and the allowlist together.

## Refactoring sequence

Use these seams for incremental cleanup rather than attempting a repository-wide rewrite:

1. Migrate `execution/` commands to `execution/_lib.py` as they are touched and
   remove each migrated command from the architecture baseline. Compose parsers
   with `command_parser()` and `add_database_argument()`; new commands leave the
   database default unset, while legacy commands preserve an old default only
   by opting into it at the call site.
2. Continue extracting cohesive route families into typed registration modules
   with explicit minimal contexts and shared route-support code.
3. Keep one canonical copy of byte-identical golden artifacts and test that all
   consuming flavors still render the same output.
4. Treat the embedded runtime in `src/pipeline/work_os_shell.py` as its own
   future extraction project; preserve browser behavior and golden coverage.
5. Before deleting apparently unused commands, prove transitive reachability
   from scripts, schedulers, tests, documentation, and Windows wrappers.

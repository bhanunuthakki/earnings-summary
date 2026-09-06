# Folder structure contract

**Class:** canonical. This document owns repository topology; the validator reads
the embedded contract below. Runtime-only directories may be absent in a clean
checkout and are created only by their named producer.

## Machine contract

<!-- folder-contract:start -->
```json
{
  "required_source_directories": [
    "alembic",
    "config",
    "cron",
    "dcf",
    "design-system",
    "directives",
    "docs",
    "evals",
    "examples",
    "execution",
    "instruction_tests",
    "micro_thesis",
    "mockups",
    "scripts",
    "src",
    "templates",
    "tests",
    "vendor"
  ],
  "required_state_directories": ["data"],
  "optional_runtime_directories": [
    ".cache",
    ".tmp",
    "_inbox",
    "ir_documents",
    "output",
    "transcripts"
  ],
  "tooling_directories": [
    ".claude",
    ".codex",
    ".design-sync",
    ".githooks",
    ".github",
    ".harden"
  ],
  "registered_exception_directories": ["outputs", "scratch"],
  "forbidden_top_level_directories": [
    "cache",
    "tmp_tests",
    "transcripts_in"
  ]
}
```
<!-- folder-contract:end -->

`execution/validate_folder_contract.py` fails when a required root is absent, an
unregistered tracked root appears, or a forbidden root exists. It ignores ordinary
tool/runtime directories such as `.git`, `.venv`, and caches.

## Ownership and lifecycle

| Root | Authority and lifecycle |
|---|---|
| `src/` | Importable business logic, application services, schemas, and UI masters. |
| `execution/` | Thin deterministic CLI entry points. No ad-hoc scripts. |
| `directives/` | Canonical contracts, runbooks, drafts, and history classified by `directive_manifest.json`. |
| `tests/` | Application tests; may use application fixtures. |
| `instruction_tests/` | Standalone instruction and hook tests; never imports `tests/conftest.py` or opens the app DB. |
| `alembic/` | Append-only migrations governing the configured canonical Windows database authority and explicit disposable migrated test databases. |
| `data/` | Durable source caches and host-owned application state. The production database location is resolved from approved Windows runtime configuration, not inferred from this checkout. In particular, a Mac checkout-local `data/portfolio.db` is an invalid artifact, not a replica or fallback; explicitly named disposable test databases and approved snapshot restores have separate authority. Preserve legitimate source caches unless a specific recovery or deletion workflow authorizes mutation. |
| `.tmp/` | Resumable intermediates, checkpoints, and disposable task state. Safe to clear only when no active run or recovery path depends on it. |
| `.cache/` | Optional reproducible cache with an explicit TTL or invalidation rule. |
| `output/` | Canonical generated application deliverables, including `output/research/<TICKER>/`. Reproducible unless a directive says otherwise. |
| `_inbox/` | Optional user drop zone. Successful intake moves content into a canonical source store. |
| `ir_documents/` | Optional durable issuer-document store, organized by ticker and period/event. |
| `transcripts/` | Optional raw/processed transcript source store. |
| `dcf/` | User-editable canonical per-ticker DCF workbooks. |
| `cron/` | Windows Task Scheduler manifests and wrappers. |
| `evals/` | LLM evaluation cases, rubrics, and versioned fixtures. |
| `design-system/`, `.design-sync/` | Design-system source and generated-sync metadata; `.design-sync/` is a registered tooling root. |
| `scratch/` | Registered compatibility exception for still-referenced one-offs and historical plans. New durable product logic is prohibited here. |
| `outputs/` | Registered artifact-tool output. It is not the application `output/` destination and must not be read by product code. |

## Rules

1. Intermediates and debug artifacts go under `.tmp/`, not a new root.
2. Product deliverables go under `output/` or a directive-declared external destination.
3. Raw evidence moves only through a typed intake path; do not silently delete it after derivation.
4. New top-level directories require updating this contract and its validator evidence in the same change.
5. Root-level scripts and memos are prohibited. Use `execution/`, `scripts/`, `docs/`, or a registered historical location.
6. A path's lifecycle comes from this contract and the owning directive, not from whether it is currently gitignored or absent.
7. On Mac, application and test commands must use an explicit temporary, restored-snapshot, or approved read-only database path. They must never create or infer live state from checkout-local `data/portfolio.db`.

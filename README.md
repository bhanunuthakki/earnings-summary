# Earnings Summary

## Overview

Earnings Summary is a solo-built, pull-only, localhost equity-research platform. Its primary interface is the Work OS command center: a local Flask application for reviewing portfolio and company research, inspecting operational state, refreshing permitted work, and managing comments, decisions, and governed research proposals.

Start with the command center at `http://127.0.0.1:7421`. The main operator workflow is to review the portfolio overview and inbox, drill into a company, inspect evidence, freshness, thesis, valuation, and open loops, run an appropriate refresh when needed, and explicitly review any proposed durable change. Rendered research reports remain useful browser artifacts, but the Work OS is the front door for current work.

The platform combines deterministic collection and computation with evidence-aware synthesis across company, portfolio, earnings, filings, investor relations, transcripts, valuation, risk, research, and operations. The canonical domain vocabulary—including coverage roles, research levels, lifecycle, and schedule classes—lives in [DEFINITIONS.md](DEFINITIONS.md).

> This repository is designed for local operation. Declared scheduler configuration is not evidence that a task is installed, enabled, healthy, recently run, or produced fresh output on this machine.

Useful references: [the operator guide](HOW_TO_USE_REPORTS.md), [the data-pipeline contract](directives/data_pipeline_dag.md), [the directives index](directives/README.md), and [the Windows scheduler runbook](cron/SETUP_WINDOWS_SCHEDULER.md).

## Quick start

### Canonical Windows runtime / product use

The canonical production-shaped code checkout and product-state database live on Windows, but they have distinct roots. The code checkout is `C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary`; the canonical database root is `C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary`, with the database at `C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\data\portfolio.db`. Run the following commands from the Windows code checkout; never create or use a relative `data\portfolio.db` inside the runtime checkout, and never use this Windows database for Mac development:

Requirements: Python 3.11 or later. From the repository root, activate your preferred virtual environment and install runtime plus development dependencies:

```powershell
$EarningsSummaryCodeRoot = 'C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary'
$EarningsSummaryDbRoot = 'C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary'
$EarningsSummaryDbPath = Join-Path $EarningsSummaryDbRoot 'data\portfolio.db'
Set-Location $EarningsSummaryCodeRoot
$env:EARNINGS_SUMMARY_DB_PATH = $EarningsSummaryDbPath

pip install -r requirements.txt
pip install -e ".[dev]"
```

Initialize a new SQLite database or safely upgrade an existing one through the guarded bootstrap seam:

```powershell
python execution/sqlite_bootstrap.py execution/upgrade_database.py --db-path $EarningsSummaryDbPath --repo-root $EarningsSummaryCodeRoot --runtime-root $EarningsSummaryCodeRoot
```

The upgrader uses the shared write lock, validates SQLite integrity, backs up versioned databases before mutation, and refuses to guess a baseline for a non-empty unversioned database. Do not replace it with an ad-hoc migration command for an operator database.

On a fresh install, mirror the checked-in holding theses into the database after the schema upgrade:

```powershell
python execution/sqlite_bootstrap.py execution/sync_thesis_state.py --db $EarningsSummaryDbPath --apply
```

Start the Work OS with the same bootstrap seam:

```powershell
python execution/sqlite_bootstrap.py execution/comments_server.py --port 7421 --repo-root $EarningsSummaryCodeRoot
```

Open `http://127.0.0.1:7421` in a browser. On Windows, [start_comments_server.bat](start_comments_server.bat) is an alternative launcher that locates a managed `venv` or `.venv` and starts the same local server.

### Mac development

Mac development and tests must use a disposable database outside this checkout. The following command creates a temporary database path and binds the server and its internal consumers to it; do not omit `EARNINGS_SUMMARY_DB_PATH` or substitute `./data/portfolio.db`:

```bash
MAC_DB_DIR="$(mktemp -d /tmp/earnings-summary-db.XXXXXX)"
export EARNINGS_SUMMARY_DB_PATH="$MAC_DB_DIR/portfolio.db"
python3 execution/sqlite_bootstrap.py execution/upgrade_database.py \
  --db-path "$EARNINGS_SUMMARY_DB_PATH" --repo-root . --runtime-root . --allow-isolated-db
python3 execution/sqlite_bootstrap.py execution/comments_server.py --port 7421 --repo-root .
```

For Mac product use against the always-on runtime, do not start a local server or open Mac `127.0.0.1:7421`. On Windows, run `tailscale serve status` and open the exact private HTTPS origin it reports. A remembered hostname, raw Tailnet IP, or the DNS name from `tailscale status` is not a substitute.

Use the command center for normal operator work. Its documented surface includes portfolio and ticker views, on-demand refreshes, report comments, research conversation, and proposal review. [How to use the workspace reports](HOW_TO_USE_REPORTS.md#command-center-start-here) is the source of truth for supported screens and request shapes.

Optional capabilities have separate dependencies or access configuration:

- Install `.[ir]` and Chromium only for IR-document discovery that needs browser rendering.
- Install `.[gsheets]` only for the DCF-to-Google-Sheets round trip.
- Configure only the provider credentials needed for intended work. Pass secrets through environment variables, never command-line arguments.

## How it works

The project separates directive, orchestration, and deterministic execution:

1. [Directives](directives/README.md) define task intent, constraints, cadence, idempotency, and failure handling.
2. Orchestration sequences work and handles outcomes without becoming a second business-logic layer.
3. [Execution entrypoints](execution/) perform typed, single-purpose work such as fetches, transformations, validation, builds, audits, and maintenance.

The canonical processing model is [INGEST → TRANSCRIBE → PARSE → VALIDATE → PERSIST → COMPUTE → SYNTHESIZE → PUBLISH](directives/data_pipeline_dag.md). Typed models govern stage boundaries. Validation failures should stop unsafe persistence rather than be guessed around; transient failures follow bounded retry policy, while authentication and schema-contract failures halt. Intermediate state belongs in `.tmp/`, and runs are designed to be idempotent and resumable.

SQLite is the local durable state store, with schema changes under [alembic/versions](alembic/versions). The coverage model is database-driven: a tracked instrument has independent coverage role, lifecycle, instrument kind, and derived schedule class. Governed portfolio and evaluation work therefore differs from watchlist monitoring, screened index members, catalog records, and archived rows. Review the [Coverage Role Resource Contract](DEFINITIONS.md) before changing onboarding, routing, or scheduled-work behavior.

### Work OS and reports

The Work OS provides status and cross-ticker analysis, per-ticker drill-down, refresh actions, and comment or thesis workflows in one local application. The per-ticker experience is intended to bring together identity, freshness, artifacts, analyses, decisions, thesis, and position context. Reports provide a durable reading and commenting surface; the operator guide explains how they connect to the command center and how proposals are reviewed before durable state changes.

The repository also contains a local research-conversation path. A model response alone is not authorization to mutate canonical thesis or KPI state: governed proposals are reviewed through an explicit approval or keep-current decision.

### LLM routing

LLM calls are centralized through `call_llm`; product code should not make direct provider calls. For normal purpose-routed traffic, the documented default is Codex-first: purpose tiers resolve to the Codex membership transport. On an operational Codex failure, the system falls back to the Claude subscription transport and records that fallback. Explicit Claude, Gemini, or OpenRouter model-family requests remain explicit rather than being silently translated.

`LLM_PRIMARY_SUBSCRIPTION_BACKEND=claude` is the documented reversible rollback switch; the documented default is Codex. Web-grounded calls use the same ordering, with a grounding gate before a result is accepted. Purpose selection, model pins, budget enforcement, ledgering, and evaluation governance are centralized; see [LLM Calls](directives/llm_calls.md).

> Provider access is optional and can be metered. The Claude CLI may use an authenticated subscription or `ANTHROPIC_API_KEY`; Gemini fallback may use `GEMINI_API_KEY` or `GOOGLE_API_KEY`; and the Anthropic SDK supports the separate message-batches lane. Configure only what the work requires.

For JSON-expecting LLM work, use the established structured-output boundary rather than treating an invalid response as an empty result. New or materially changed LLM purposes must follow the purpose registration, prompt-version, and evaluation workflow in [LLM Calls](directives/llm_calls.md).

## Operations

The Work OS is the normal place to inspect and initiate work. Prefer its per-ticker refresh and review workflows over manually assembling long script chains. The documented refresh dispatcher supports stale or full modes and selected steps; its jobs are single-flight per ticker. Read [the command-center refresh guidance](HOW_TO_USE_REPORTS.md#refreshes--with-overrides) before using force or budget-bypass controls.

Automation is declared in [cron/task_manifest.json](cron/task_manifest.json); [cron/TASKS.generated.md](cron/TASKS.generated.md) is the generated human-readable inventory. Treat the manifest as the source of truth for declared task definitions, not as evidence of live registration or execution.

Validate scheduler artifact generation against the manifest with:

```powershell
python execution/sqlite_bootstrap.py execution/generate_cron_artifacts.py --check
```

Compare the declared manifest with the live Windows Task Scheduler state with:

```powershell
python execution/sqlite_bootstrap.py execution/verify_cron_registration.py
```

Installation and registration are separate operator actions described in [the Windows Task Scheduler runbook](cron/SETUP_WINDOWS_SCHEDULER.md). The runbook also documents wrappers, logging, backup and restore practices, and schedule definitions. Use the verifier rather than inferring live state from repository files.

Run only one process per mutable database state, cursor, output artifact, or write set. Scheduled and interactive work must honor the same run-lock boundaries. If a multi-step pipeline fails, inspect its recorded outcome and `.tmp/` state, then resume from the applicable checkpoint rather than casually restarting work that may already have persisted valid results.

For report-oriented work, use [HOW_TO_USE_REPORTS.md](HOW_TO_USE_REPORTS.md). It is the maintained operator reference for command-center actions, local reports, comments, proposal review, and manual refreshes; this README intentionally does not duplicate its volatile command inventory.

## Development

Read [AGENTS.md](AGENTS.md) before changing code. It defines the repository architecture, deterministic-execution boundary, concurrency rules, UI contracts, data-handling expectations, and repository-specific operating constraints.

Keep changes narrow and preserve the separation between `src/` business logic and thin `execution/` CLIs. Reuse existing primitives for network access, parsing, storage, and LLM calls. Put deterministic transformations in code rather than orchestration prose. Execution scripts are expected to validate typed inputs and outputs, keep structured logs separate from stdout data, and remain idempotent for the same inputs.

Use the [Makefile](Makefile) as the canonical developer command surface. During active work, run:

```powershell
make check-fast
```

Before handoff or pre-push, run the complete gate:

```powershell
make check
```

The complete test suite is available through:

```powershell
make test
```

The repository has intentional pre-existing whole-tree lint and type-check baselines. The enforceable local gates focus on changed files, formatting, type checks, and tests; the [Makefile](Makefile) defines the exact target behavior. For frontend or report-renderer changes, follow the additional UI-control and golden-render requirements in [AGENTS.md](AGENTS.md).

When changing data collection or schema behavior, preserve provenance, validate source data, and make failure paths explicit. Do not silently merge conflicting sources, infer currencies or periods, or bypass the guarded database path. Before removing legacy code, check transitive reachability from current entrypoints so a local cleanup does not sever active imports.

### Keeping this README current

Preview a repository-evidence-based README candidate without writing `README.md`:

```powershell
python execution/sqlite_bootstrap.py execution/update_readme.py
```

After review and approval, perform the guarded atomic write:

```powershell
python execution/sqlite_bootstrap.py execution/update_readme.py --apply
```

The updater collects a bounded allowlist of repository evidence, retains candidates and judgments under `.tmp/readme_updater/`, validates repository-local Markdown links, and refuses to overwrite a README changed after evidence collection.

## Security

Keep `.env`, `credentials.json`, and `token.json` out of version control and logs. Supply secrets through environment variables; never put them in CLI arguments, checked-in configuration, generated artifacts, issue text, or exception output.

Use [src/log_redact.py](src/log_redact.py) as the canonical secret-redaction helper when handling credential-bearing URLs, provider errors, or exceptions. Network and document-processing paths handle untrusted material: preserve source provenance, validate parsed data, respect source policy and rate limits, and halt on authentication or schema-contract failures instead of retrying blindly.

The command server is intended for loopback use. Its CORS behavior is restricted to local origins; changing the bind interface requires an explicit `COMMENTS_SERVER_CORS_WHITELIST`. Review the server options in [execution/comments_server.py](execution/comments_server.py) before changing its host or access posture.

Back up and verify a production-like SQLite database before schema work. The guarded upgrader is the supported seam for database changes. Backup, restore, and scheduler operations are operationally consequential: use their documented runbooks and verification commands, and do not treat a checked-in configuration file as proof of a successful live operation.

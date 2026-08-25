# Weekly Cleanup

## Goal

Bound growth of disposable local artifacts and expire stale research proposals
without deleting research evidence, recovery material, or active pipeline state.
The filesystem phase is allowlist-only and dry-run-first. Database mutation is
separately guarded by the checkout-to-database Alembic revision preflight.

## Target sources

- `.tmp/cron_logs/`: regular files older than 30 days.
- `.tmp/cron_runs/`: regular files older than 30 days.
- `.tmp/news_cache/*.json`: entries whose payload `cached_at` is older than
  seven days. Invalid or missing timestamps are retained.
- `.tmp/pdf_pages/`: regular files older than 30 days.
- Python cache files under `src/`, `execution/`, `tests/`, `cron/`, `scripts/`,
  and `alembic/`, plus root `.pytest_cache/` and `.ruff_cache/`: entries older
  than seven days.
- `.tmp/temp_audio_*`: inventory only. Deletion remains owned by
  `execution/qa_transcripts.py` and requires a matching `qa_status=ok`.
- `research_tasks`: `execution/expire_stale_research.py --apply` applies the
  existing two-packet/never-packeted expiry policy only after schema preflight.

## Authorized tools

- `execution/run_weekly_cleanup.py`
- `execution/expire_stale_research.py`
- `cron/run_python.bat` and the shared `runtime.job_runtime` lock/health seam

No network or LLM calls are authorized.

## Output schema

`run_weekly_cleanup.py` emits JSONL decision events to stderr and one
Pydantic-validated JSON object to stdout:

- `policy_version`
- `idempotency_key` (legacy serialized field name for the Logical Idempotency Key)
- `mode`
- aggregate `files_scanned`, `would_delete`, `deleted`, `bytes`, and
  `skipped_invalid`
- per-policy counts, including unsafe, QA-unverified, and error skips

Any eligible file that cannot be deleted produces `skipped_error` and a nonzero
exit. A filesystem-cleanup failure prevents the research-expiry stage.

## Cadence, identity, and repeat safety

- Refresh cadence: Sunday at 13:00 America/Los_Angeles.
- **Logical Idempotency Key:**
  `weekly_cleanup:{ISO-year-week}:{policy-version}`.
- **Content Identity:** digest of the canonical policy and eligible-target inventory
  recorded by the decision receipt.
- **Observation Version:** bounded filesystem/database evidence time and inventory
  digest for the sweep.
- **Attempt Identity:** unique job-runtime invocation and its receipt; retries change it.
- Re-running the same policy after a successful application finds no eligible
  files until new artifacts cross their retention boundary.
- Rate-limit budget: zero network requests and zero LLM calls.

## Failure-mode policy

- Missing allowlisted roots are successful no-ops.
- Invalid news-cache JSON/timestamps are retained and counted.
- Symlinks, junctions/reparse points, `state.json`, and `job_locks` are retained.
- Unlink/stat errors are logged and fail the filesystem stage.
- A database revision mismatch fails before any research-task mutation.
- Task Scheduler uses `IgnoreNew`, a 15-minute limit, one retry after 30
  minutes, and `StartWhenAvailable=false`.

## Explicit exclusions

Never traverse or delete:

- `.git/`, `.claude/`, `venv/`, `.venv/`, or `node_modules/`
- `data/`, `output/`, `transcripts/`, or `ir_documents/`
- database files, WAL/SHM files, backups, migrations, source files, directives,
  credentials, tokens, keys, or certificates
- active checkpoints, indexes, lock guard files, or unverified temporary audio

Output archive retention and backup retirement require separate,
provenance-aware owner approval and are not part of this recurring task.

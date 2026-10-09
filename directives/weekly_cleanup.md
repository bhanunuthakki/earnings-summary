# Weekly Cleanup

## Goal

Bound growth of disposable local artifacts and expire stale research proposals
without deleting research evidence, recovery material, or active pipeline state.
The filesystem phase is allowlist-only and dry-run-first. Database mutation is
separately guarded by the checkout-to-database Alembic revision preflight.

This runbook implements the intermediate lifecycle in `data_pipeline_dag.md` and
the scheduled-operation rules in `operations_governance_surface.md`. It owns the
allowlist and execution mechanics, not a second retention policy or schedule.

## Target sources

- `.tmp/cron_logs/`: regular files past the canonical disposable cutoff, except active
  checkpoint trees and recovery material.
- `.tmp/cron_runs/`: regular files past the canonical disposable cutoff, except active
  checkpoint trees and recovery material.
- `.tmp/news_cache/*.json`: entries whose payload `cached_at` is past the
  canonical cache cutoff. Invalid or missing timestamps are retained.
- `.tmp/pdf_pages/`: regular files past the canonical disposable cutoff, except active
  checkpoint trees and recovery material.
- Other regular files under `.tmp/`: entries past the canonical disposable cutoff, excluding
  owned policy roots, active checkpoint trees containing `state.json`, locks,
  database/backup/recovery material, and unverified temporary audio.
- Python cache files under `src/`, `execution/`, `tests/`, `cron/`, `scripts/`,
  and `alembic/`, plus root `.pytest_cache/` and `.ruff_cache/`: entries past
  the canonical cache cutoff.
- `.tmp/temp_audio_*`: inventory only. Deletion remains owned by
  `execution/qa_transcripts.py` and requires a matching `qa_status=ok`.
- `research_tasks`: `execution/expire_stale_research.py --apply` applies the
  existing two-packet/never-packeted expiry policy only after schema preflight.
- `data/operations/artifact-retention.json`: explicit file registrations for
  completed test copies and superseded verified backups. The catalog specifies
  hashes, allowed scopes, family, status and recovery pins. Its scopes are excluded
  from the ordinary age sweep. Failed, active, unverified and pinned entries stay.
- `.earnings-temp-run.json`: per-run producer receipts discovered under state
  and runtime `.tmp` roots, and narrowly named `earnings-summary-*` runs under
  `C:\tmp` and the user temporary directory. Verified declared test files expire
  seven days after successful closure. Active, failed, pinned, malformed, changed
  and undeclared files remain. Pytest produces these receipts automatically.
- Operational SQLite backups in state `data/backups`: discover exact snapshot
  manifests and join them to sealed KPI repair/disposition attempt and backup
  restore-readiness receipts in the matching state operation directories.
  Successful apply closure, canonical source identity and verified bytes are
  required. Keep the latest completed verified backup per source and purpose.
  Failed, unfinished, unknown and pinned backups remain visible as held. A later
  compatible success can resolve a failed attempt on the next pass. No database
  is opened for this discovery. Deployment snapshots and other unsupported
  producers still require an explicit operator catalog.
- State and runtime roots receive the same owned policies: logs, run outputs,
  news caches, PDF page images, and Python tool caches. Runtime ordinary temporary
  files also receive the existing 30-day window. Both roots retain source documents,
  code, secrets, credentials, tokens, keys, certificates, and archives. These
  exclusions apply during collection, before unlink, and during directory pruning.
  A copied checkout with a `.git` file or directory is held as a whole tree.
  Cleanup never follows that Git metadata. Loose JavaScript modules, SQL files,
  DLL files, registry exports, encoded fixes, financial tables, research documents
  and source patches are also held. Source and recovery checks inspect every
  filename suffix, so renaming a source or backup file does not make it disposable.
  Source-root Python caches remain eligible under their separate seven-day policy.
  Both state and runtime catalogs protect their registered scopes and siblings;
  the state catalog retains priority when registrations overlap.

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
- `coverage`: logical bytes and file counts by root, run, disposition and file-age
  bucket (0–14, 15–30, 31–60 and over 60 days). This read-only inventory includes
  held environments, registered scopes, undeclared siblings, and the state
  `data/operations` and `data/backups` recovery roots. It reports `incomplete`
  when unknown or unreadable paths remain. File modification age does not establish
  session activity or grant deletion authority.
  On Windows it also counts all `C:\tmp` legacy test trees, including names that
  lack producer ownership receipts. This extends visibility only; it does not
  add those trees to the deletion sweep.
- `operational_backups`: classified backup discovery and hold reports. Unknown,
  invalid or incomplete evidence prevents a complete coverage claim. Missing
  retired bytes are not counted as reclaimed space.

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
- Symlinks, junctions/reparse points, active or unrecognized checkpoint trees,
  and `job_locks` are retained. A checkpoint is completed only when its
  `state.json` object has a recognized terminal `status` (`complete`,
  `completed`, `done`, `success`, or `succeeded`); completed trees then receive
  their owning policy's canonical disposable window. Malformed state fails closed.
- Genuine unlink/stat errors are logged and fail the filesystem stage. Recognized
  read-only Windows files are protected skips; the cleaner does not remove their
  read-only attribute. Other eligible artifacts can expire while those files stay.
  Dry-run uses the same current identity, ownership, checkpoint, catalog,
  read-only and hardlink checks before it counts a deletion candidate.
- A database revision mismatch fails before any research-task mutation.
- The newest timestamped log for each job stays. Failure detection reads the full
  log, so an early unlink error cannot disappear behind a long successful tail.
  Failure logs stay, including any nonzero `skipped_error` or `exit_code` and
  malformed or unrecognized operational result fields. A newer success does not
  resolve an earlier failure. Reviewed successful old logs can expire.
- Read-only Windows artifacts stay unless a separate exact-file decision permits removal.
- All checkpoint ancestors must be completed; an inner completed state cannot
  override an outer active, failed or malformed state.
- Task Scheduler uses `IgnoreNew`, a 15-minute limit, one retry after 30
  minutes, and `StartWhenAvailable=true`. A missed slot can resume when the host
  is available. The retry is bounded recovery for a transient failure. Persistent
  permission or configuration failures remain visible and their artifacts stay;
  retry does not authorize attribute changes or weaker protection.

## Explicit exclusions

Never delete through the ordinary age sweep:

- `.git/`, `.claude/`, `venv/`, `.venv/`, or `node_modules/`
- `data/`, `output/`, `transcripts/`, or `ir_documents/`
- database files, WAL/SHM files, backups, migrations, source files, directives,
  credentials, tokens, keys, or certificates
- active checkpoints, indexes, lock guard files, or unverified temporary audio

Output archive retention is outside this task. Backup retirement requires an
explicit hash-bound catalog or supported producer evidence that creates an
equivalent exact-file declaration. The owner's October 7, 2026 cleanup request authorizes
classified old test copies and superseded backup files, with the latest verified
backup per family and unresolved failure recovery retained. This authority does
not cover raw source evidence or application state. The existing weekly job
applies the catalog through `src/operations/artifact_retention.py` on its existing
filesystem-maintenance lane. Research expiry keeps the portfolio-db lane.

`src/operations/operational_backup_retention.py` discovers supported closed
operations on every pass. It does not alter the operator catalog. Exact snapshot,
readiness and immutable attempt digests accompany each declaration. Changed or
missing evidence holds both retirement targets and surviving copies. Unknown
legacy backups never become eligible from their filename or modification time.
The operation-directory membership and JSON byte digests are bound before and
after discovery, then checked again before deletion. New failed records or a
changed `latest.json` require replanning. Producer failure holds are preserved
when operator registrations overlay a plan. Exact existing registrations can
supply a legacy family name for future copies; conflicting mappings between
source/purpose families stay held.
The same job retries transient deletion failures with its existing bounded retry;
the next invocation replans from current evidence and preserves prior receipts.

The cleanup has no automatic clearing date for unclassified legacy temporary
trees. Review coverage by decreasing byte size. Establish the operation owner,
completion and recovery needs before registering exact disposable files. Keep
small closure, failure and retirement receipts after clearing large data copies.
Completed managed test files clear on the first Sunday pass more than seven days
after completion. Ordinary eligible temporary files use their 30-day cutoff.

The cleanup resolves the configured product state root. A code checkout is not
the production artifact root. If runtime `data/` is a junction or symlink whose
metadata names exactly configured state `data/`, the cleanup uses only the state
catalog. It checks this alias before any child catalog probe. The state target
and its ancestors must be real, unlinked directories. Windows metadata prefixes
`\\?\` and `\??\` receive lexical normalization; this does not resolve a linked
child. Foreign, broken or unreadable aliases fail before deletion. Independent
real runtime data directories keep their own catalog. The selected authorities
govern startup, explicit retirement and catalog refresh before ordinary deletion.
Each selected catalog checks existing ancestors before testing catalog presence.
A linked ancestor fails closed even when the child catalog is missing.
Runtime `.tmp/` producer discovery remains independent of the data alias.
Protected scopes use a hashed ancestor lookup. Replacing catalog scopes rebuilds
that lookup before the next protection check. This stores path membership only;
each deletion still checks current catalog authority, file identity, links and
checkpoint state. Recovery files protect their entire top-level temporary tree.
Completed registered test files use seven-day
retention; other completed temporary artifacts retain the 30-day window.
The read-only coverage inventory can traverse excluded environment and recovery
directories to count their bytes. It never follows symlinks or reparse points,
opens databases, reads source documents, or authorizes new deletion scopes.
`--code-root` selects the runtime source root for a supervised dry run. The
scheduled entrypoint uses its own installed runtime root and the configured state
root. There is one weekly cleanup writer and no additional scheduled job.

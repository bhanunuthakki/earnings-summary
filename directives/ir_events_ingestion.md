# Directive: Forward IR Events Ingestion (BHA-15)

## Status and scope

This Layer-1 SOP governs deterministic discovery, validation, and persistence of public,
forward-dated IR events for active tracked companies. The outcome is a source-linked,
provenance-backed Forward agenda with explicit freshness state.

This directive authorizes only its own draft. Implementation, migration, live writes,
Scheduler registration, commit, and push require separate approvals. There is no LLM leg.

The current schema is not activation-ready:

- `record_investor_day` commits each row instead of joining a caller-owned batch;
- `signals` lacks stable event identity, event kind, lifecycle revision, and source-
  observation linkage;
- `ux_signals_event` cannot represent distinct same-day events; and
- `INSERT OR IGNORE` cannot reconcile reschedules or cancellations.

The migration and batch-writer gate below must land before any live apply.

## Supported event kinds

`EventKind` is closed to:

- `investor_day`
- `analyst_day`
- `capital_markets_day`
- `strategy_day` — only when an official IR source explicitly makes it investor-facing.

These project to `signals.signal_type='investor_day'` while retaining their canonical
kind. Exclude earnings dates/calls/webcasts, conference appearances, shareholder
meetings, product events, regulatory or clinical milestones, podcasts, past events, and
undated notices. A keyword alone is insufficient. Never infer or synthesize an event from
cadence, prior years, estimates, search snippets, or LLM output.

## Authoritative source hierarchy

Use the highest available tier; lower tiers may corroborate but not override:

1. `publisher_event_authority` — verified issuer-owned event feed, API, or archive,
   represented by `IRAuthorityEvidence` and captured through the existing IR authority
   and evidence-ledger path.
2. `issuer_ir_announcement` — issuer-owned public IR page or release explicitly naming
   the event and date, with verified issuer/host identity and immutable raw capture.
3. `issuer_regulatory_announcement` — original issuer filing or exhibit from the official
   regulator explicitly naming the event and date.

Mirrors, aggregators, news, search results, and generic crawls are not authoritative.
No third-party vendor is selected. Adding one requires current first-party review of
capability, limits, pricing, licensing, and provenance. No qualifying source means
`unsupported`, not empty.

## Typed contract

Use strict, frozen Pydantic-equivalent models with `extra='forbid'`.

```python
EventKind = Literal[
    "investor_day", "analyst_day", "capital_markets_day", "strategy_day"
]
EventStatus = Literal["scheduled", "rescheduled", "cancelled"]
SourceTier = Literal[
    "publisher_event_authority",
    "issuer_ir_announcement",
    "issuer_regulatory_announcement",
]
AttemptStatus = Literal[
    "ok", "not_found", "robots_denied", "rate_limited", "access_denied",
    "contract_error", "transient_error", "unsupported",
]
Disposition = Literal[
    "inserted", "replayed", "superseded", "cancelled", "rejected", "conflict"
]
RunStatus = Literal["complete", "empty", "partial", "error", "disabled"]
Freshness = Literal["fresh", "stale", "unavailable"]

class IREventObservation(BaseModel):
    event_id: str
    revision_id: str
    supersedes_revision_id: str | None
    issuer_id: str
    ticker: str
    event_kind: EventKind
    status: EventStatus
    title: str
    event_date: date
    starts_at: AwareDatetime | None
    source_timezone: str | None
    source_tier: SourceTier
    source_event_id: str | None
    source_url: str
    source_observation_id: str
    raw_sha256: str
    authority_surface_revision_id: str | None
    source_published_at: AwareDatetime | None
    observed_at: AwareDatetime

class IRSourceAttempt(BaseModel):
    ticker: str
    source_tier: SourceTier
    source_url: str
    status: AttemptStatus
    http_code: int | None
    latency_ms: int | None
    record_count: int
    source_observation_id: str | None

class IREventDisposition(BaseModel):
    event_id: str
    revision_id: str
    disposition: Disposition
    reason_code: str
    signal_id: int | None

class IREventRunResult(BaseModel):
    schema_version: Literal["ir-events-run.v1"]
    run_id: str
    mode: Literal["dry_run", "apply"]
    status: RunStatus
    freshness: Freshness
    as_of: AwareDatetime
    calendar_date: date
    roster_sha256: str
    policy_sha256: str
    checkpoint_path: str
    attempts: tuple[IRSourceAttempt, ...]
    events: tuple[IREventObservation, ...]
    dispositions: tuple[IREventDisposition, ...]
    inserted: int
    replayed: int
    superseded: int
    cancelled: int
    rejected: int
    conflicts: int
```

All digests use sorted compact UTF-8 JSON. `event_id` is
`ir-event:v1:<sha256([issuer_id,event_kind,stable_source_identity])>`, where stable source
identity is a publisher event ID or canonical event-specific URL. Reject a generic
calendar URL without a stable publisher event ID. `revision_id` hashes the event ID,
normalized payload, and source observation. Identical evidence replays; changed date,
title, status, or evidence creates a linked revision.

## Pacific date and admission rules

- Use `America/Los_Angeles` and `calendar_today()` for the business date.
- Preserve an explicit date-only source value as a civil date without UTC conversion.
- A timestamp requires an offset or explicit IANA zone; store the instant in UTC and
  derive `event_date` in Pacific time.
- Reject a clock time without a zone.
- `observed_at` and `source_published_at` are timezone-aware UTC instants.
- Require canonical ticker-to-issuer binding, explicit kind/date in captured bytes,
  uncredentialed HTTPS, matching raw hash, and a stable source identity.
- Accept dates from Pacific `calendar_date` through `calendar_date + 548 days`.
- Never repair an implausible year, timezone, identity, or title by inference.

Deduplicate by `event_id`. A reschedule supersedes the current revision; cancellation
retains canonical history and removes only the active projection. Absence from a
non-exhausted surface is not cancellation. Higher source tier wins while retaining
conflict evidence. Same-tier conflicts produce no current projection. Distinct event IDs
on the same ticker/date remain distinct.

## Pre-activation migration and writer gate

1. Add append-only `ir_event_revisions` with the observation fields, positive revision
   sequence, unique revision ID and `(event_id, revision_sequence)`, hash/status/clock
   constraints, and foreign keys to the prior revision and
   `evidence_source_observations`.
2. Add nullable `ir_event_id` and `ir_event_revision_id` links to `signals`. Replace the
   linked-event ticker/date uniqueness with partial uniqueness on `ir_event_id`; retain
   the legacy date guard only for unlinked legacy rows.
3. Add typed `record_ir_events_batch`. It validates the complete batch, persists evidence
   and revisions, reconciles projections, and commits once. Make
   `record_investor_day` a no-commit compatibility wrapper or retire it after migration.
4. Any failure rolls back the full database batch. A committed revision and projection
   must reference the same durable, hash-verified source observation.

The active projection uses `signal_type='investor_day'`, `cadence='scheduled'`,
`source_feed='ir_events'`, authoritative title/date/URL, and issuer name in `firm`.

## Cadence, network, and access

Target cadence is one active-roster sweep daily at 06:30
`America/Los_Angeles`, plus explicit `--ticker` dry-runs. This is not an activated
schedule. Registration requires separate approval and a fresh collision audit; never use
the protected 03:00–05:00 window.

Per run:

- one in-flight request per host and at most 10 requests/minute/host;
- at most 25 surface requests/ticker and 500 responses/full roster;
- 10-second connect, 60-second read, 5 same-host redirects, and 25 MB/surface;
- publisher limits and `Retry-After` override these ceilings.

Fetch and honor `robots.txt` before discovery, cache the decision for 24 hours, and count
that request against the budget. `robots_denied` is terminal. Use public unauthenticated
surfaces only; reject URL credentials, stored cookies, cross-host/unsafe redirects, WAF
bypass, and SSRF targets. Stop on 401/403. Apply the shared URL guard and log redactor.

Queue `source_calls` telemetry with `kind='ir_events'`; it is best-effort observability,
not provenance. Event truth requires immutable raw bytes and a source-observation link.

## Checkpoint, resume, and lock

Use `.tmp/ir_events/<run_id>/state.json`, atomically replaced after each surface/ticker.
The Logical Idempotency Key is
`ir_events_<Pacific date>_<roster sha12>_<policy sha12>` for a sweep. The roster and
policy digest is a Content Identity; source publication/observation fields and
raw-response digests form each Observation Version. `run_id` is the Attempt Identity
and appends a unique start-time/random suffix to the logical key; it changes on retry.
Checkpoint hashes,
completed source keys, cursors, raw paths/hashes, typed candidates, telemetry, and ticker
status. Resume only within 36 hours with matching hashes; otherwise start a new run.
Never repeat a completed source key. Output over 100 KB or 2,000 lines goes to the run
directory; stdout returns its path and typed summary.

Discovery may parallelize read-only across tickers. Apply is one serialized batch:
scheduled entrypoints use `JobLock(PROJECT_ROOT, 'ir-events-ingest',
['portfolio-db'])`; interactive library use uses
`hold_run_lock(db_path, owner='ir-events-ingest')`. Never nest both or write unlocked.

## State semantics

- `complete/fresh`: all required surfaces reached terminal success within 36 hours.
- `empty/fresh`: the same proof with zero future events; unsupported tickers prevent a
  globally empty result.
- `partial`: any failed or unsupported source; retain successful updates and prior events
  for failed tickers, and never render empty.
- `stale`: last complete receipt is older than 36 hours; render retained events with the
  last-success timestamp.
- `error/unavailable`: no complete receipt, unreadable store/schema, or hard stop; map to
  `data-calendar-state='unavailable'`, not empty.
- `disabled`: apply is off; dry-runs do not advance live freshness.

Past events leave the active agenda but remain in canonical history.

## Failures and circuit breaker

- Timeout, reset, 429, or 5xx: honor `Retry-After` or use bounded exponential backoff.
  Allow three retries after the initial failure; the fourth consecutive failure
  checkpoints and halts.
- Schema, parse, pagination, content-type, identity, or hash error: no unchanged retry;
  retain raw bytes and redacted reason in the checkpoint and project nothing.
- 401/403: halt immediately without retry or bypass.
- Robots denial: make no request to the denied path.
- Plausibility/conflict failure: quarantine the candidate and project nothing unresolved.
- Lock, writer, or integrity failure: roll back the full batch and exit non-zero.

Stderr is redacted JSONL; stdout is typed result data only.

## Activation, disable, and rollback

Activation requires:

1. reversible migration and batch writer validated on a migrated DB clone, including
   same-day events and injected mid-batch rollback;
2. fixed-seed coverage for all kinds, Pacific boundary/date-only handling, ambiguous
   time, replay, reschedule, cancellation, conflicts, tier precedence, robots/auth,
   empty/stale/unavailable, and cursor resume;
3. a full-roster live dry-run reconciled to captured bytes, issuer identity, and stable
   event identity;
4. separate owner approval for one supervised live apply, followed by receipt-to-row
   parity, quick/FK/duplicate/current-revision checks, and rendered-calendar verification;
5. separate scheduling approval plus proof of the real registered task, command/runtime
   revision, last result, health record, lock ownership, and a successful scheduled
   receipt.

`IR_EVENTS_APPLY_ENABLED` defaults to `0`; `--apply` requires exactly `1`. Disable by
resetting it to `0` and separately disabling any registered task. Apply receipts list
revision and signal IDs. Rollback defaults to a plan, requires explicit mutation approval,
holds the writer lock, and retires only receipt-owned active projections/revisions in one
transaction. Never delete immutable source observations or raw evidence. Re-enable only
after the defect is fixed and activation proof is repeated.

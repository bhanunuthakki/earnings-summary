# Consolidated Roadmap — 2026-08: Debt & Simplification First, Then Improvements

Consolidates three 2026-08-03 planning sessions into one sequenced plan:

1. **Data-infra audit** → `directives/data_infra_audit_2026_08.md` (PR #1164, merged). Detail
   authority for Track A phases A1–A4; this file sequences, that file specifies.
2. **Security audit + debt roadmap** (session-only; findings preserved below — they were never
   committed anywhere durable).
3. **P0→P3 roadmap planning** (session-only) — produced both the improvement backlog (Track B
   below) and the debt-first operating principles this file adopts.

**Sequencing ruling (owner-ratified intent): technical debt + simplification of codebase AND
functionality first, improvements second.** Three amendments the sources force:

- **Security fixes and "stop the bleeding" items come before simplification** — they are small,
  independent, and current behavior lies or is exposed. They are Phase A0, not part of the debt
  campaign proper.
- **Resolve owner-gated deletion policy before mutating retained data.** The
  `latest_governed_*` ruling is now resolved as deletion (2026-08-08); the remaining
  `fmp_snapshots/` age-thinning decision still gates retention work because it changes
  as-of granularity.
- **A few items filed as "improvements" are actually truth/foundation debt and move into
  Track A**: poisoned July eval invalidation, Sunday-job verification, and the Home
  request-scoped read connection. Pure capability work stays in Track B.

## Operating principles (from the P0→P3 session; they govern every Track A PR)

1. **ONE way to do each thing.** Where two systems coexist, finish the cutover AND delete the
   loser in the same campaign. Activation is the means; deletion is the point. Success metric:
   net negative lines, fewer parallel systems.
2. **Killed means gone.** Build a census of killed/superseded features (diet news, coach_strip,
   Commitments badge, saydo_due, wondering_detect, edit-splice, TCC freshness, #1130 archive
   scheme, Portfolio page kills, …) and excise remaining pipelines/routes/tables/crons/CSS.
   Every excision cites the ruling that killed it; no deletion without a citable ruling —
   ambiguous cases go on the owner-decisions list.
3. **Documentation must equal reality.** Stale directives are executable misinformation in an
   agent-driven repo. Sweep `directives/`, `platform_backlog.md`, design docs against code and
   prod DB. Directive edits need owner authorization — batch the diffs into one request.
4. **Indexes are enforced, not curated.** One canonical registry per tier (surfaces → endpoints
   → actions → analyses → LLM purposes → jobs → read-models), each generated from or reconciled
   against code by CI. The purpose-registry sync guards / `REGISTERED` surface set / quota-window
   registry are the working precedents. A hand-curated list is prose debt. Self-test: delete a
   route and CI must go red on the index guard alone.
5. **Ratified opinions become enforced invariants.** A ruling without a guard is debt.
6. **Owner decisions are surfaced, never made.** One cumulative list (bottom of this file).

---

## Track A — Debt & simplification

### Phase A0 — Security fixes + stop the bleeding (independent small PRs, first)

**Security (from the 2026-08-03 audit; locations + intent — nearly every fix is "apply a guard
that already exists elsewhere at the one site that skipped it").** Systemic caveat: the cockpit's
CSRF/Origin guard validates the Origin header's *shape*, not caller identity — with
`--tailscale`/`0.0.0.0` any Tailnet peer can spoof it. That multiplies H2/H3/M2; don't redesign
the guard as part of these fixes.

| # | Finding | Where | Fix intent |
|---|---|---|---|
| H1 | Unauthenticated SSRF: Telegram → artifact fetch (only fully-unauthenticated-remote hole — do first) | `src/research/artifact.py:150,212`, `src/capture/poller.py:513` | Route fetch through `ir_pipeline/_net`; sender/chat allowlist in `poll_once` |
| H2 | Path-traversal write `/chat/<ticker>` (Windows `..%5C`) | `src/chat_session.py:65` | `safe_ticker` at helper + route, like DCF/comments routes |
| H3 | Stored XSS: comment `status`/`intent` bypass `Literal` enum → `innerHTML` | `src/comments.py:196`, PATCH route, `workspace_comments.py:443` | `validate_assignment=True`, validate PATCH body, escape fields |
| H4 | Path-traversal write: EDGAR `accessionNumber` | `src/etf_sources/nport.py:369,385` | Validate accession shape (reuse `download.py` sanitizer) |
| M1 | Gemini redaction gap: raw `str(exc)` | `src/llm/cli.py:1716`, `gemini_backend.py:355,361` | `redact()` one-liners |
| M2 | DCF GET path-traversal (read-only) | `execution/comments_server.py:3553,3562,3867` | `safe_ticker` |
| M3 | CORS echoes `null` origin on GET reads | `comments_server.py:645`, `src/server_runtime/access.py:56` | Gate `null`-origin GETs behind capability header / Fetch-Metadata |
| M4 | `javascript:`-scheme link XSS | `src/ui/cite_marks.py:345,353`, `src/pipeline/source_viewers.py:226` | http(s) allowlist on `href` |
| M5 | `/approve` GET CSRF residual | `comments_server_alert_routes.py:61` | Default-deny when `Referer` + `Sec-Fetch-Site` both absent, or POST |
| LOW | Missing `redact()`; token-in-path | `src/compute/peer_selection.py:559`, `src/capture/telegram.py:224` | Add `redact()`; extend it to mask path-borne tokens |

PR grouping: H1 alone; H2+H3+M2 together (cockpit validation one-liners); M1+LOW together
(redaction sweep); H4/M3/M4/M5 as they fit. Regression grep-gates: unvalidated `<ticker>`→FS,
bare `requests.get` outside `_net`, `str(exc)` in `src/llm/**` logs. Clean — don't re-hunt:
SQLi, command injection, deserialization, XXE, TLS/crypto, committed secrets.

**Stop the bleeding (data-infra Phase 0 remainder — see that directive for specs):**
already DONE: GC-sidecar backup coverage (#1159), backup-cadence root cause + retention
(#1128/#1148/#1155, 2026-08-02), lock unification (#1172/#1174), backup staging fix (#1173).
Still open:

- Drive-snapshot freshness alarm (<48h → cron-health red) and verify `backup_file_gc.py` is
  actually *scheduled* (chained after `backup_db`), not just correct.
- Wrapper exit-tail fixes (`run_backfill_earnings_surprises.bat`) + manifest wrapper-content
  assertion.
- WAL-size tripwire in cron-health.
- Verify `onboard_pending` per-ticker lock scope (#1174) actually ended the hourly-failure
  signal; if Last Result is still red, keep triaging.
- `theme_synth.py:103` bare `except Exception` → hard-stop propagation.
- Manifest/doc reconciliations (task count, UTF-16 re-encode, runtime-path `<Command>`, 03:00
  vs 03:40).

**Truth debt (moved from Track B):** the eight-day eval-freshness alarm and durable,
fail-closed weekly sweep receipt are shipped. Operational closure remains open until a current
graded cohort succeeds and the transport-poisoned July verdicts are re-run or invalidated.

### Gate — Owner decisions (make before Phase A2 starts)

1. **[RESOLVED — DELETE, 2026-08-08] `latest_governed_*` plane.** Migration
   `0002_drop_dead_tables` removed the unread schema and the governed deletion catalog removed
   the unreachable code/tests. `docs/hardening/L1/deletion_recovery_receipt_2026_08_08.json`
   records production-derived population, integrity/FK, and recovery evidence. Track B
   activation is void; do not rebuild or repopulate this plane.
2. **[DECISION] `fmp_snapshots/` age-thinning** (1.2 GB, 30.7k files, grows forever) — changes
   as-of granularity; owner sign-off required.

### Phase A1 — Launcher unification (data-infra Phase 1 remainder)

Lock unification is DONE (#1172/#1174). Remaining:

- Route the 7 root `.bat` entrypoints through `cron/run_python.bat`; delete `full_refresh.bat`
  in favor of `refresh_dispatch.py --mode full`.
- Unshadow the 04:00 window (`scan_ir_transcripts`, `backfill_transcripts` structurally starved).

### Phase A2 — Delete the dead 20% + killed-feature census (biggest LOC win)

The census (principle 2) is the first population pass of the tier indexes (principle 4) — one
inventory, two outputs. Then execute data-infra Phase 2:

- `src/provenance/` scaffolding ~17k LOC (start with the 12.5k imported by nothing).
- ~60 `execution/` scripts: 9 zero-reference, the deleted latest-governed cutover cluster,
  31 one-shot backfills → git-tracked `execution/archive/` + `# ARCHIVED` convention
  (exempt from Layer-3 conventions) for anything that must stay auditable.
- Dead schema: 9 zero-reference tables, 7 write-only tables, unread views incl. the `_v2`
  family (gated where they overlap the decision). Mind the FTS/batch_alter trigger trap.
- CI check: fail on any table/view with no `FROM`/`JOIN` in product code.
- Killed-feature excisions from the census, each citing its ruling, each landing a guard.

Every deletion PR: confirm zero product readers (grep + tests green), one cluster per PR, full
suite before push, never piped through head/tail.

### Phase A3 — Collapse the bases (shared primitives, monolith, migrations, docs, guards)

- **Migration squash** (data-infra 3.1) → **fixture migration** (3.2; 177 files replaying the
  262-step chain) → **one DB-path resolver** (3.3). Squash lands first; add
  `next_alembic_number` tooling to kill the head-collision ritual while in there.
- **`comments_server.py` decomposition**: five explicit route clusters now own 43 routes
  (content 24, DCF 7, alerts 6, settings 4, journal 2); the core retains 111 routes behind an
  exact 154-route runtime contract. Continue extracting only cohesive clusters over explicit
  contexts; route-count reduction alone is not a reason to split a deep module.
- Shared primitives (data-infra 3.4–3.7): `src/net/client.py` + `FmpClient`;
  `execution/_lib.py` + convention test; one `store_conn()`; `connect_sqlite` as sole policy
  authority.
- **Docs = reality sweep** (principle 3): directives, `platform_backlog.md`, SHIPPED stamps,
  O6 passages — batched into one owner-authorization request.
- **Rulings → guards** (principle 5): close the §4 guard gaps (reinvented components,
  geometry), extend kit ratchets, retire quarantine lists by fixing contents, encode canonical
  readers (`dcf/latest`, freshness) against bypass. (DEFINITIONS.md action vocabulary landed —
  #1170.)
- Flip-and-delete every half-flipped dual-read/feature switch (inventory first; #988 v1
  transport parity is the known example).

### Phase A4 — Lifecycle & data-quality hardening (data-infra Phase 4, unchanged)

Retention owners (`ir_documents/`, `.tmp/`, absolute paths in `documents.file_path`);
`financial_facts` pre-persist plausibility gate; FMP fallback-rung validation; shared row-drop
helper with drift threshold; generate the 44 cron wrappers from `task_manifest.json`;
declarative resumable morning pipeline; `extract_*` consolidation. Split `integrity_audit.py`
(6.3k lines) only *after* the gate decision resolves.

---

## Track B — Improvements (starts once A0–A2 are done; A3/A4 may interleave)

Re-verify each item is still open before working it; this repo moves ~150 commits/week.
Strategic frame: **activation over construction** — an item is done when consumed in
production, not merged.

**B1**
- ~~`latest_governed` activation~~ **VOID — owner chose deletion on 2026-08-08; migration 0002,
  the deletion catalog, and the recovery receipt record A2 closure.**
- Home render: thread one request-scoped read connection through the strip renderers (measured
  ~51 connections/~625 statements per render; cockpit leg is the precedent). Record per-leg
  timings for the B2 cache decision.
- Disclosure D4, ratified order: Ask grounding → workspace strip.
- One real mutation→promotion cycle (`prompt_pin_overrides`) with a non-Anthropic judge on the
  audit rung.
- Registry coverage to ~90% of spend (check current first), then delete scaffold derivation.
- Investment-Partner adoption review: keep/fix/kill proposal — analysis, not a build.
- Operator batch for the owner: GEMINI key rotation, Drive backup convergence check, supervised
  Telegram brief run, decision-draft triage (77 unconfirmed).

**B2**
- Cascade pilot (quality program P3 — the only unbuilt phase).
- Steady-state Home cache: re-measure after the B1 connection fix; if still material, owner
  ruling first (non-GET cache clearing is load-bearing for mutation correctness — TTL can't
  just be lengthened).
- Golden sets to n≥8–10 for thin purposes; run the never-run judge spot-check.

**B3**
- ETF-sleeve stress treatment (after the FLKR ruling); disclosure O9/O10 residuals; FPI
  crosstab blind spot with the next SEC-ingest touch; FMP Starter consequences either way.

---

## Explicitly out of scope / leave alone (merged from all three sources)

- Age-deleting `kpi_facts`/`llm_calls` — standing policy.
- Postgres / replacing SQLite (`postgres_shadow` gets deleted in A2, not revived).
- Universal WAL checkpoint CLI — tripwire only.
- The 9 duplicated-state reconcilers — deliberate self-healing; delete *unused* planes only.
- Per-entity DCF `build_*` scripts and the `llm_client`/`llm/*` shim — deliberate, not debt.
- Data-infra "do not churn" list (`data_infra_audit_2026_08.md` §healthy) applies throughout.

## Cumulative owner-decisions list

1. `[RESOLVED 2026-08-08] latest_governed_*`: delete; do not reactivate.
2. `fmp_snapshots/` age-thinning (gate for A4 retention work).
3. Directive-edit authorizations (batched diff from the A3 docs sweep).
4. Migration-squash go/no-go ruling (A3, destructive-adjacent: needs cited backup + restore drill).
5. Home cache re-prime / TTL ruling (B2).
6. FLKR/ETF bear exemption (B3).
7. FMP Starter purchase (B3).
8. Ambiguous kills surfaced by the A2 census.

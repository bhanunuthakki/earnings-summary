# Codebase debt and hardening audit

Authorized remediation is tracked separately in [the implementation receipt](2026-09-19-hardening-implementation.md). The findings below describe the audited snapshot.

Date: 2026-09-19. Audited revision: `b87cbf50f13add156a8dc5b092f05673c6a5704b`.

## Recommendation

Start with the migration recovery path, report security, comment preservation, and database-path authority. Then simplify fixtures and validation gates, remove proven dead helpers, and consolidate duplicated business rules. These changes address actual failure mechanisms and maintenance cost.

Do not begin with another migration squash or a broad rewrite. The repository already has a squashed baseline: **39 active revisions and 264 archived revisions**, each with one head and no missing parent references. The main migration debt is the compatibility behavior around those graphs.

This is a recommendation audit, not an implementation or release approval. No product code, database, credentials, scheduler, or live service was changed. The only audit artifact is this report. Existing work was initially dirty and was committed externally during inspection; findings were reconciled against the clean revision above.

At final verification, concurrent edits appeared in `execution/backfill_evidence_ledger.py`, `src/provenance/evidence_backfill.py`, `tests/test_evidence_backfill.py`, and a new `tests/test_evidence_backfill_cli_guard.py`. Those later edits are outside this audit snapshot and were left untouched.

## Coverage and evidence

The inventory parsed **2,802 Python files / 1,050,774 lines** across `src`, `execution`, `tests`, `alembic`, `cron`, `scripts`, and `instruction_tests`. It included all 1,356 Python files in the production `src`/`execution`/`cron` trees. Broad AST, reference, duplication, and configuration scans were followed by targeted manual tracing. This is not a claim that every line received manual review.

| Check | Result |
|---|---|
| AST parse of the inventoried Python population | No syntax errors |
| `.venv/bin/python -m ruff check --no-cache src execution scripts cron tests --statistics` | Exit 0; no diagnostics |
| `.venv/bin/python scripts/check_architecture_boundaries.py` | Passed |
| `.venv/bin/python execution/validate_directive_manifest.py` | Passed; 83 documents |
| `.venv/bin/python execution/validate_folder_contract.py` | Passed |
| Migration graph inspection | Active head `0039_add_dcf_forecast_series`; archived head `0273_post_earnings_readout_budget` |
| Isolated source-function probes, using synthetic stubs | Reproduced null-origin allowance, destructive bridge call sequence, HTML script breakout, corruption-to-empty conversion, and cache repopulation ordering |
| Full pytest, Pyright, dependency CVE scan, live/browser/load tests | Not run in this audit |

The probes executed extracted functions or the pure cache module; they did not import the application, access real comment files, run migrations, create databases, read secrets, or start a listener. The HTML probe confirmed an additional script element, not JavaScript execution in a browser. The first probe assertion expected an unescaped attribute value and failed; a corrected attribute-free payload confirmed the same extra-element result.

The checked-in static quality ceilings declare **4,022 Pyright diagnostics and 3,053 suppression findings**. These are recorded allowances, not freshly measured failures. Architecture allowances contain 365 entrypoint path-mutation entries and 92 loose root modules. These counts identify maintenance concentration; they are not individual defects.

Formal L1 hardening preflight returned **HOLD** because its capability registry has no current qualified reviewer receipts. Package closure and the preflight's narrow credential-shaped tracked-diff check passed. That narrow check is not a whole-history secret audit. No maturity advancement or specialist certification is claimed. This does not prevent the code-evidence recommendations below.

## Highest-priority fixes

### H1 — Migration recovery can erase retained rows

**High severity; high confidence. Owner: migration/recovery.**

[`upgrade_database.py:313`](/Applications/earnings-summary/execution/upgrade_database.py:313) handles an allegedly current schema restamped to an archived revision by validating `operation_events`, stamping `ACTIVE_HEAD`, and downgrading to `0011_add_operations_journal`. It then reanchors and replays upgrades.

That downgrade traverses destructive operations introduced after 0011: [`0039:259`](/Applications/earnings-summary/alembic/versions/0039_add_dcf_forecast_series.py:259) drops forecast points/mappings, and [`0017:114`](/Applications/earnings-summary/alembic/versions/0017_add_owner_decision_checkpoints.py:114) drops owner checkpoint tables. Recreating tables does not restore their rows. The verified-backup requirement aids recovery but does not make this an acceptable successful upgrade.

The source-function probe recorded the stamp → downgrade-to-0011 → restamp sequence. The existing [bridge test](/Applications/earnings-summary/tests/test_upgrade_database.py:511) uses an empty current database and checks revision/receipt/backup, so it misses retained-row loss.

**Change:** fail closed on this metadata/schema mismatch. Permit a metadata-only repair only after complete schema identity is established. Remove the broad downgrade from recovery.

**Proof:** seed current forecast/checkpoint/receipt rows, restamp to the archive, and assert either rejection without mutation or exact row preservation. Exercise recovery on isolated databases only.

### H2 — Report boot JSON permits stored HTML script breakout

**High severity; high confidence. Owner: report renderer.**

[`boot.py:63`](/Applications/earnings-summary/src/report/renderers/workspace_sections/boot.py:63) embeds raw `json.dumps(payload)` inside a script element. JSON escaping does not protect HTML raw-text parsing. A comment containing `</script><script>void 0</script>` creates an additional executable script element when the report is rendered. The isolated probe reproduced this through the actual source function with synthetic comment data.

Normal comment-display escaping and authorized comment writes do not protect this embedding boundary. Pasted source text and generated resolutions can carry the delimiter.

**Change:** use one HTML-safe JSON encoder for embedded payloads, escaping `<` as `\u003c` while retaining exact original text in storage.

**Proof:** render closing-script strings in every free-text field; parse HTML to verify no extra elements and decode JSON to verify exact round-trip content.

### H3 — Null-origin reads can disclose the mutation capability

**High severity; high confidence in server behavior, browser exploitability unverified. Owner: browser access policy.**

[`access.py:57`](/Applications/earnings-summary/src/server_runtime/access.py:57) unconditionally permits `Origin: null`. The [capability guard](/Applications/earnings-summary/execution/comments_server.py:885) exempts GET, and the response hook echoes the permitted origin. The [report route](/Applications/earnings-summary/execution/comments_server_content_routes.py:472) serves HTML containing the stable report capability embedded at [boot.py:39](/Applications/earnings-summary/src/report/renderers/workspace_sections/boot.py:39).

Where browser local-network policy permits a request, an opaque-origin page can read private responses, potentially retrieve that capability, and satisfy the mutation guard. Loopback filtering identifies the connecting machine; it does not identify the page running in its browser. MDN explicitly warns that sandboxed documents can have null origins and that allowing them is unsafe. [MDN CORS header reference](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Access-Control-Allow-Origin).

**Change:** preferably serve interactive reports from the exact approved application origin. If file-based interactive reports remain required, require capability proof on sensitive null-origin reads as well as writes, including report HTML; preserve safe preflight behavior. Use exact allowed origins rather than automatically trusting every loopback port. Add explicit allowed-Host enforcement at the same boundary.

**Proof:** reject unauthorized null-origin private API/report GETs; preserve authorized report reads/writes; reject unexpected Host values. A browser canary must distinguish application rejection from browser mixed-content/local-network blocking. No remote exploit was executed.

### H4 — Corrupt comment files are silently replaced after the next append

**High severity; high confidence. Owner: comment storage.**

[`comments.py:239`](/Applications/earnings-summary/src/comments.py:239) converts parse/read errors into an empty store. [`append_comment:439`](/Applications/earnings-summary/src/comments.py:439) appends to that store and saves over the original. Atomic writes and locks protect concurrent writes, but cannot preserve data that the reader has already discarded.

**Change:** distinguish missing, loaded, and invalid/unreadable stores. Mutation must reject the last state and preserve original bytes. Rendering may show an explicit degraded state.

**Proof:** malformed JSON and incompatible-schema fixtures must survive a rejected append byte-for-byte. The isolated reader probe already confirms corruption currently becomes a successful empty load.

### H5 — Code root, data root, and database authority are still conflated

**High severity; high confidence. Owner: runtime configuration and consumers.**

Active consumers still infer `repo_root/data/portfolio.db` despite the external canonical database contract:

- [DCF assumptions](/Applications/earnings-summary/execution/dcf_opus_assumptions.py:46) and [workbook builder](/Applications/earnings-summary/execution/build_redesigned_dcf.py:65) pass checkout-derived paths.
- [Segment overrides](/Applications/earnings-summary/src/compute/segment_cache.py:72) return raw records if that path is missing or unreadable, silently omitting corrections.
- [Report construction](/Applications/earnings-summary/execution/build_artifacts.py:246) mutates global DB settings to a root-derived location.
- [Workspace panels](/Applications/earnings-summary/src/report/renderers/workspace_data.py:1094) independently reconstruct the path and degrade missing data to empty results.

**Change:** resolve the approved database path once at the entrypoint and pass it explicitly, alongside separately named `code_root` and `state_root`. Thread an existing connection through composite reads where supported. Distinguish unavailable authority from a valid empty result. Reuse existing runtime configuration and resolver interfaces.

**Proof:** use a synthetic database outside the checkout with distinctive overrides, notes, and assumptions. All DCF/report paths must read it; unavailable authority must produce a clear error or persisted degraded status. No checkout-local database may be created.

### H6 — Historical fixtures falsely advertise the current schema

**High severity as a verification defect; high confidence. Owner: test fixtures.**

[`conftest.py:253`](/Applications/earnings-summary/tests/conftest.py:253) and [`:540`](/Applications/earnings-summary/tests/conftest.py:540) rewrite an archived database's revision to the active head without applying active migrations. Twenty-four test files explicitly request reanchoring. Schema equivalence with the original squashed baseline no longer means equivalence with today's head.

The parity test compares archived builds with archived copies and a changed revision string, rather than proving parity with the current active schema. Runtime guards consequently accept fixtures missing newer tables/constraints.

**Change:** keep historical fixtures historical; application tests use the cached active fixture. Where a bridge itself is under test, run the real supported bridge. Remove global reanchoring as each dependent test is converted.

**Proof:** compare schema semantics and required seed data between genuine active and bridged databases; retain explicit historical migration tests. Do not weaken writer guards to accommodate inaccurate fixtures.

## Reliability, speed, and update friction

### M1 — One migration-head authority and accurate drift messages

**Medium severity; high confidence. Owner: migration tooling.**

[`upgrade_database.py:39`](/Applications/earnings-summary/execution/upgrade_database.py:39) hardcodes the head; [`schema_compat.py:43`](/Applications/earnings-summary/src/schema_compat.py:43) derives it from the graph. The upgrader's [early return](/Applications/earnings-summary/execution/upgrade_database.py:435) uses the constant. Forgetting one update can falsely report an old database as current.

Additionally, [`schema_compat.py:268`](/Applications/earnings-summary/src/schema_compat.py:268) diagnoses known archived revisions as newer-than-checkout revisions, although the upgrader explicitly supports them.

**Change/proof:** derive the head once from the configured graph; distinguish active-behind, archived-upgrade-required, and unknown revisions. Verify a synthetic extra migration without editing a second head constant. Keep fixed revision names in tests that intentionally exercise historical states.

### M2 — DCF orchestration discards failure status

**Medium severity; high confidence. Owner: DCF CLI.**

[`build_all_redesigned_dcf.py:87`](/Applications/earnings-summary/execution/build_all_redesigned_dcf.py:87) discards the assumption-refresh return code and builds anyway after an explicit `--opus` request. Builder success also depends on a stdout prefix rather than the process status.

**Change/proof:** make nonzero exit status authoritative; distinguish success, intentional skip, and failure. Test failed refresh, nonzero builder with misleading `RESULT` output, and legitimate skip. If stale assumptions are an allowed fallback, require an explicit policy and visible result rather than silent continuation.

### M3 — Consolidate local, hook, and CI quality checks

**Medium severity; high confidence. Owner: developer tooling.**

[`Makefile:34`](/Applications/earnings-summary/Makefile:34) selects `BASE...HEAD`, excluding uncommitted and untracked work from the fast-loop changed-file checks. Local formatting is line-based, [pre-push](/Applications/earnings-summary/.githooks/pre-push:55) permits existing per-file lint counts, and [CI](/Applications/earnings-summary/.github/workflows/ci.yml:440) requires whole changed retained files to be clean. Some Make targets use bare PATH tools despite resolving a project interpreter.

**Change:** one shared changed-file selector and quality runner with explicit worktree/commit modes. Use the project interpreter consistently. Keep lightweight local and full CI modes, but make their rules identical for the files they check.

**Proof:** cover staged, unstaged, untracked, deleted, renamed, and committed files; compare selected populations and pass/fail behavior. Report omitted checks explicitly. Run relevant existing tests when production code changes even if no test file was edited.

### M4 — Fix cache invalidation ordering before adding more caching

**Medium severity; high confidence in the race, not load-tested. Owner: server response cache.**

[`start_request_timer`](/Applications/earnings-summary/execution/comments_server.py:799) invalidates cache families before mutation. A concurrent GET can reserve and store old data after that eviction but before commit. There is no general post-success eviction, so the old snapshot remains for the 30-second TTL. The pure cache probe reproduced that ordering.

**Change/proof:** invalidate after successful commits, using the existing family registry; give background mutations an explicit completion invalidation. Retain generation protection for older in-flight builds. Use synchronization barriers to test the mutation/GET interleaving and unaffected-family caching.

### M5 — Bound admitted chat work and preview allocation

**Medium severity; high confidence in missing bounds, impact unmeasured. Owners: streaming and PDF preview.**

- [Chat submission](/Applications/earnings-summary/execution/comments_server.py:742) uses a four-worker executor with an unbounded submission queue; response threads wait on `Queue.get`. A bounded output queue does not bound pending requests. Add an active-plus-pending admission limit with prompt overload responses and tested release on success/failure. Preserve accepted durable exchanges.
- [PDF preview](/Applications/earnings-summary/src/pipeline/pdf_render.py:163) rasterizes untrusted page dimensions at fixed DPI before imposing a pixel limit. Bound dimensions/pixels before allocation. Consider a resource-limited preview worker only if document measurements justify it. Test extreme synthetic page boxes and normal readability.

### M6 — Replace misleading migration-cost rules with measured fixture policy

**Medium severity; high confidence. Owner: test infrastructure and instructions.**

[`test_suite_migration_cost.py:33`](/Applications/earnings-summary/tests/test_suite_migration_cost.py:33) counts files containing the text `command.upgrade`, not full-chain executions or their duration. A [one-step test](/Applications/earnings-summary/tests/test_squashed_seed_recovery_migration.py:33) uses `getattr(command, "upgrade")` to avoid that spelling-based scanner. Historical timing claims do not establish present costs after the squash.

The [implementation trap](/Applications/earnings-summary/directives/agent_implementation_traps.md:8) also tells clean fixtures to invoke upgrade directly, while the repository and cached fixture instructions tell them to use `migrated_db`.

**Change/proof:** one clear rule: cached active fixtures for ordinary tests; direct replay for explicit migration/parity tests. Reuse the existing AST-based test-DB inventory where suitable; do not introduce another governance subsystem. Measure slow fixtures before and after conversion. Correct contradictory instructions and retire obsolete timing prose.

## Removal and consolidation candidates

These have stronger evidence than “no static import.” Reference checks included tests, CLI/scheduler paths, registry/dynamic patterns, and whole-repository symbol searches. Repeat them at deletion time because the checkout is active.

| Candidate | Recommendation and evidence | Required preservation |
|---|---|---|
| [Old portfolio SQL readers](/Applications/earnings-summary/src/report/sections/portfolio_position.py:199) | Remove roughly 190 lines: `_holding_accounts`, `_recent_transactions`, `_open_decisions`, `_closed_decisions`, and their private date helper. The live builder uses `resolve_configured_position`; only old private-reader tests retain some helpers. | Move meaningful snapshot/date assertions to canonical adapter/build tests first. |
| [Legacy Ask parser](/Applications/earnings-summary/execution/comments_server.py:3321) | Remove unused `_parse_ask_turn`; the active session-aware parser follows it. | Existing session/request parsing coverage. |
| [Unused auth helper](/Applications/earnings-summary/execution/refresh_cache.py:190) | Remove `_prepare_fmp_auth`; current recovery uses the credential decision interface. | Credential-path tests and redaction. |
| Other unused private helpers | Candidates: `thesis._kpi_definition_meta`, `heterogeneous_retrieval._bundles_from_members`, `segment_oi_10k._parse_period_ends`, `population_metric_ontology._earliest_clock`. Together with the preceding two helpers, approximately 110 lines. | Recheck dynamic references and run subsystem tests; never delete decorators/validators based on ordinary reference counts alone. |
| [Disconnected news ladder](/Applications/earnings-summary/src/news/news_ladder.py:1) | Retire the 113-line test-only policy or reconcile its required behavior into the active owner. Its `websearch_fallback` label disagrees with `news.store`'s `websearch_opus`, and it omits `yf_news`. | Preserve required source policy; do not accidentally activate stale restrictions. |
| Four copies of `_locate_form_10k` | Consolidate `segment_definitions:154`, `segment_crosstabs_llm:402`, `company_description:241`, and `analyze_filing_intelligence:98`. Their bodies match; platform diagrams already import one private copy. | Exact year/latest year/missing file behavior. Prefer direct path lookup for a known year. |
| [Duplicate fiscal cadence logic](/Applications/earnings-summary/execution/dcf_opus_assumptions.py:66) | Reuse `dcf.fiscal_periods.detect_fy_periods`, already used by the workbook builder. | Quarterly, semiannual, partial-year and short-history tests. |
| Dormant feature modules | Investigate `research/drift.py`, `research/dcf_tweak.py`, `pipeline/mobile_inbox_panel.py`, `pipeline/cc_state.py`, and `dcf/fact_sheet.py`. The scan found tests/registry/documentation references but no runtime caller. | Product decision and external/manual-use check before retirement; update reconstruction/design records. These are not unconditional deletions. |

Do not indiscriminately delete `src/provenance`, archived migrations, registry-loaded extractors, subprocess entrypoints, or small typed statement wrappers. Those have live reachability or recovery/semantic responsibilities that a simple unused-import scan misses.

## Names and definitions worth improving

| Current ambiguity | Better boundary |
|---|---|
| `repo_root` means code, data, and database authority depending on caller | Explicit `code_root`, `state_root`, and resolved `db_path`; prioritize H5. |
| `research_tasks.run_id` stores JSON, `cost_usd` stores an estimate | Use existing API fields `metadata` and `estimated_cost_usd`; move [session context's direct SQL](/Applications/earnings-summary/execution/session_context_pack.py:284) behind the store. Keep physical columns until a substantive schema change needs migration. |
| DCF `T`, `inc`, `cf`, `pseg`, `prof`, `m`, `idx` | `ticker`, `income_records`, `cashflow_records`, `segment_records`, `company_profile`, `to_millions`, `index_fiscal_records`. Preserve unit-bearing financial names. |
| `dcf_opus_assumptions.py` / `--opus` names a provider | Name the operation “refresh DCF assumptions”; provider selection already belongs to purpose routing. Preserve a temporary CLI alias only for actual callers. |
| `[]`, `None`, or raw source records represent both absence and failure | Typed result/status at important read boundaries: present, legitimately empty, unavailable, invalid. Start with comments, overrides, and decision-facing readers. |
| “Canonical” news enum disconnected from production | Keep one source vocabulary beside the actual store/ingestion policy. |

Avoid cosmetic database migrations and a repository-wide renaming campaign. Rename as the owning boundary is simplified and keep stored identities stable.

## Changes that reduce rule burden

The 83 directives are already classified: 21 canonical, 29 runbooks, 28 history, and 5 drafts. Active canonical/runbook documents total 5,713 lines; history/drafts total 8,399. Preserve the manifest's single-owner mechanism. Remove contradictions and stale narratives rather than adding more layers.

Specific cleanup: the fixture conflict in M6; old “253 active migrations” prose in `schema_compat`; old pre-squash CI bootstrap commentary; `requirements.txt` claims about “CI installs latest” despite hash locks; and obsolete provider/batch comments. Update these at their existing source of authority. Do not add another parallel rulebook.

The 5,296-line `comments_server.py` and its 4,650-line `create_app` are maintenance concentration, not automatically a performance defect. Continue the existing route-registration pattern by cohesive feature area, after behavior fixes and route-contract tests. The 2,788-line embedded `_production_runtime` in `work_os_shell.py` similarly deserves a separately testable browser module when that interaction next changes, with rendered parity evidence. Neither warrants a framework rewrite solely to reduce line counts.

## Suggested delivery order

1. **Preserve state:** H1 and H4, each with a failing preservation regression and isolated recovery proof.
2. **Close report boundary defects:** H2 and H3, preserving required file/report workflows and adding browser evidence.
3. **Make financial inputs explicit:** H5 and M2; verify the full report/DCF path using external synthetic state.
4. **Make upgrade evidence trustworthy:** H6, M1 and M6; consolidate fixture semantics and derive migration head once.
5. **Make updates cheaper:** M3, then proven helper/news removals and shared fiscal/file-discovery logic in small independent changes.
6. **Improve measured responsiveness:** M4/M5 and composite connection reuse. Measure cold/warm route latency, connection count, pending chat work, preview pixels, and slow fixture duration before promising speedups.

Keep the existing provenance resolver, immutable financial observations, explicit database authority, verified backups, source acquisition protections, and LLM tool isolation. They protect the product's central research contract. Their implementation can be simplified without removing the guarantees.

For future migration pruning, first prove every supported database and restore path can cross a retained baseline. Alembic's documented baseline/stamp approach assumes the database schema actually exists; changing its revision label is not a schema upgrade. [Alembic cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html#building-an-up-to-date-database-from-scratch).

External references were accessed 2026-09-19. MDN supports the browser-origin recommendation; Alembic supports baseline lifecycle guidance. Neither establishes live exploitability, production data loss, or measured performance in this application. Those remain bounded verification tasks identified above.

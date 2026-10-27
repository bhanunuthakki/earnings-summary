# DCF report synchronization dependencies

Status: verified dependency map for the completed implementation. All four
implementation tranches (A, B, C, D) are complete, passing all deterministic
checks and audit test probes locally.

Authority: task checkpoint `1e2d5e3010a1d8f64938d1cfafd39ccc79698ded`, clean merge
`647833f6c268cf2499485dfc1bcd6a9ef0d2a5f5` of `origin/main`
`4920fb1c09cf61be5b9e3cbf39b2e6397345f7c0`. No merge conflicts. This map was
confirmed from the existing inventory and the named owners; it does not add product
scope.

## Model artifact ownership

- `src/dcf/artifact_promotion.py` — `hold_dcf_artifacts` owns the `JobLock` write set
  `dcf-TICKER` under the resolved `repo_root/dcf`.
- `execution/refresh_dcf.py` — Save and Refresh both use that lock owner.
- `src/dcf/persist.py` — owns the exact SQLite save receipt and artifact promotion.
- `src/dcf/meli_model.py`, `src/dcf/meli_inputs.py`, and
  `execution/build_meli_platform_dcf.py` — MELI owners.
- `execution/build_nu_platform_dcf.py` — NU owner for the existing Assum/mirror path.
- `RedesignInputs` cannot validate the NU or MELI family models; those require their
  existing family-specific owners. No new formulas are introduced here.

## Report index ownership

- `src/report/artifacts.py` — `hold_report_index` owns the cross-process `JobLock`
  `report-artifact-index` under the resolved `output/research`. It wraps index
  mutations across the complete read/modify/write.
- Approval types and the shared resolver exist. Atomic publication append is
  implemented via `publish_report_candidate` in `src/report/artifacts.py`.

## Report generation and source pinning

- `src/report/builder.py` — `build_report` threads `ReportGenerationContext` and the
  DB connection across all sections.
- `src/report/generation_context.py` is fully wired into report generation and section builders.
- `src/report/sections/snapshot.py` — the latest-model query pins exact `run_id`.
- `src/report/sections/thesis.py` — reads captured holdings, signals, and break-rules.
- `src/report/sections/synthesis.py` — reads latest `llm_artifacts` via `read_current`.
- `src/report/sections/bear_case.py` — consumes pinned inputs and binds cache keys
  to exact consumed dependency identities.
- `src/report/renderers/workspace.py` — `render_report_body` and
  `load_workspace_p3_panels` use the same captured context and DB snapshot.
- `src/provenance/source_read_context.py` — owns `SourceReadContext`; it is reexported
  from `dcf/input_evidence`. The builder and financials own source authority. A DB
  path alone grants no source authority.

## Publication lock order

Required order for publication:

1. Model artifact owner (`dcf-TICKER` for a DCF model).
2. Applicable mutable source-file fences.
3. DB writer gate and a SQLite writer-excluding transaction.
4. Report index (`report-artifact-index`).

Do not acquire upstream owners from inside the index critical section. A read
snapshot is not a writer fence. Generation and network calls stay outside the short
publication locks.

## Windows source fence

- `src/compute/evidence_snapshot.py` — `open_windows_evidence_handle` opens a file
  with share mode READ only (denying write and delete). Directory handles do not
  provide that fence.
- Wired in publication file fences with POSIX cooperative file locks. Unsupported
  or unfenced owners fail closed.

## Existing public and historical consumers

- `latest_report_artifacts`, `latest_report_artifacts_for_tickers`, and
  `work_os_briefs` are the existing public consumers.
- Company desk `_latest_brief` and `/reports/TICKER` share one selection authority
  via index-only resolution (`latest_brief` in `work_os_company.py`).
- Historical and NU replay uses approval-clock eligibility plus a pinned historical
  model.

## Resolved dependency boundaries

- Candidate generation is isolated from persistence in `execution/build_artifacts.py`.
- Fenced atomic publication append is implemented in `src/report/artifacts.py:publish_report_candidate`.
- Source-context pinning is enforced across all report sections via `ReportGenerationContext`.
- Exact consumed-dependency binding for bear-case TTL cache is enforced.
- POSIX/Windows source fencing and write-set locks are wired via `hold_capture_write_set` and file capture sessions.
- Unified current-report selection is complete across public and historical surfaces via index-only authority.
- Architecture-size growth ratchet is tracked, clean, and passing.

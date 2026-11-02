# Canonical reader initialization evidence

Assessment at `b5cca798`. Application and test source files are unchanged. The proposed structural implementation remains subject to the owner's review gate.

## Result

A fresh connection can construct `CanonicalFactResolutionEngine`, read a **resolved derived selection** through `_stored_as_known`, and then call `read_as_known` without a prior `FactReadModel` or `FactPlaneV2` on that connection. The latter call initializes its own fact reader and returns the selected derived value **3201**, with its derivation seal. This is a positive canonical read, not an absent or degraded response.

## Static dependency closure

`src/provenance/canonical_fact_resolution.py`:

- Constructor, line 209: stores the connection, executes `PRAGMA foreign_keys = ON`, and registers its own `fact_sha256` function.
- `_stored_as_known`, line 561: normalizes clocks, reads the stored revision, calls `_verify_universe`, `_verify_relation_set`, and `_verify_resolution`, then constructs `ResolutionReceipt`.
- `_verify_universe`, line 1576: reads candidate revisions/seals/dispositions; checks counts, order, canonical JSON and Python SHA-256.
- `_verify_relation_set`, line 1634: reads relation revisions/seals/assertions; checks counts, order, JSON and Python SHA-256.
- `_verify_resolution`, line 921: reads the revision, linked input seals, and the prior revision when applicable; checks Python hashes, JSON and parent linkage.

The only `self` calls in this early closure are the three verification methods. Other local calls are `_utc`, `_time`, `_sha` and `_json`; these normalize datetimes or use `hashlib`/`json`/model serialization in Python. `ResolutionReceipt` is a typed data model. None of this closure constructs a fact reader/plane or references a fact-plane-only SQL function. SQL operates on canonical resolution tables; it does not execute graph writes.

The four functions registered only by `FactPlaneV2` are `fact_cell_semantic_identity_v3`, `fact_anchor_payload_v1`, `fact_observation_payload_v1`, and `fact_derivation_basis_v1`. Its fifth registration, `fact_sha256`, is also registered independently by the engine. This distinction matters: removing an outer reader does not make construction side-effect free.

`read_as_known`, line 497, reads all stored receipts **before** constructing `FactReadModel` at line 528. That constructor creates `FactPlaneV2` and registers its five connection functions. Derived lineage traversal occurs after this initialization. The pre-initialization methods do not branch on reported versus derived selection.

Existing precedent: `src/sources/dcf_statements.py::read_dcf_statements`, line 349, constructs the engine and calls `read_as_known` at line 378 without a preceding `FactReadModel`.

## Runtime proof and cost

The task-owned wrapper reuses `tests/test_financial_source_repairs.py::test_ytd_subtraction_publishes_sealed_quarter_and_keeps_original`. It runs that existing test and fixture unchanged, then opens a fresh connection to the same explicit `migrated_db` database. No new seed graph, migration helper, database copy, or application API was added.

On that fresh connection:

1. `PRAGMA function_list` confirms all four fact-plane-only functions are absent.
2. An SQL authorizer would reject any call to those four functions. Engine construction and the first `_stored_as_known` still return the fixture's resolved derived selection. All four functions remain absent. The trace records **8 SELECT statements** and **no SQL function calls** for this stored-resolution read.
3. With the proof authorizer removed, `read_as_known((derived_cell,), REVIEW_AT)` returns the same selected derived observation and its non-null seal. The four functions are now registered by the owning reader. The read and subsequent report probe produce **zero durable row changes**.

The cutoff is `2026-07-27T12:00:01+00:00`. The derived observation and seal suffix is `a62a573eb706a25bb5746e4de0352bcb8462adf214bdbea5102f0d8187701248`. Its relation tuple is empty. Nonempty derived relations are not proved by this run.

Reproduction:

```sh
PYTHONPATH=src:. /Applications/earnings-summary/.venv/bin/python .tmp/verify_reader_initialization.py
```

Final result: **1 passed in 3.14s**. The elapsed test time includes fixture setup and the original test; it is not reader latency or a speedup measurement. The eight SELECTs describe this revision-one stored read, not every revision or the complete reader. No report verification counts or discovery SELECT baseline were measured here.

Artifacts: `.tmp/verify_reader_initialization.py`, `.tmp/reader-initialization-proof.log`, and `.tmp/reader-initialization-report-baseline.json`. The test process has exited; the existing fixture owns database cleanup. No task-owned database copy or running process remains.

## Report boundary and retained baseline

The additional unchanged `read_financial_table(fresh, 'SYNTH', as_of=REVIEW_AT)` probe reaches exactly one matching derived cell with the same observation and non-null derivation. It is **unavailable**, with `ambiguous_table_coordinate`; table-level reasons are empty. This existing fixture supports comparison of complete report output/reasons and a reached derived path. It does not prove a usable derived report value, report performance, or candidate parity before implementation.

The earlier `test_fact_read_model_batch.py::_derived_publication` experiment proves complete scalar/required-batch bundle equality only. It has no report admission proof. These two evidence scopes must remain distinct.

The report baseline JSON SHA-256 is `b79033ab93432060002a9c939b108de515d8dfe81e686ed4a1632bb1232dd04d`. A later candidate run must retain this baseline and use a separate output destination before comparing it.

Source SHA-256 values:

| Source | SHA-256 |
|---|---|
| `canonical_fact_resolution.py` | `3347673f508acbe0a7c985a30f91f28a2b5fc4ae73e832f9ebc73352d13e1025` |
| `fact_plane_v2.py` | `54a358fa2739a4d9047ccf844cd2bd22b7c0b6ac822c5735ea90291edec49bcc` |
| `sources/dcf_statements.py` | `5f6b9fcdff36d8c280076040cdbfe179e125ceefbd3a92506a31b5f9f9e5e746` |

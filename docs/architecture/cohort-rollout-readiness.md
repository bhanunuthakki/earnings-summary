# Cohort rollout evidence census

`execution/audit_cohort_readiness.py` shows the evidence gaps for an explicit
issuer cohort. It reads one SQLite snapshot in a read-only transaction. It does
not collect documents, call providers, write receipts, or run a consumer audit.

Use a disposable migrated database or an approved restored snapshot on Mac.
Resolve any live host operation through the existing host procedure first.

```sh
.venv/bin/python execution/sqlite_bootstrap.py execution/audit_cohort_readiness.py \
  --db-path /absolute/path/to/approved-snapshot.db \
  --issuer-id issuer-example-a \
  --issuer-id issuer-example-b \
  --knowledge-cutoff 2026-10-22T00:00:00Z \
  --observed-through 2026-10-22T00:00:00Z
```

Replace the example IDs with canonical issuer IDs. The command does not infer
ticker bindings. Unknown or not-yet-observed issuer IDs fail closed. The CLI
rejects missing, directory, and checkout-default database paths before opening.
Its JSON output names the exact issuer and clock scope.
Exit code 2 means the census found missing, unknown, or failed evidence. Exit
code 0 means no census blocker was found. Neither code certifies readiness.
`readiness_evaluated` and `consumer_audit_executed` remain false.

| Stage | Existing evidence owner | What the census shows |
| --- | --- | --- |
| Acquisition | `reporting_document_scope`, inventory and coverage ledgers | Latest inventory count, incomplete inventories, reporting/excluded/unresolved document counts, and source coverage statuses |
| Extraction | Processing obligation and disposition ledgers | Current obligation count, sealed terminal status counts, and obligations without a sealed disposition |
| Semantic admission | V2 fact plane, publication ledger, canonical metric cells | Issuer-specific cells and observations, observations with a publication seal, and canonical metric cell count |
| Consumer readiness | Population cutover receipts | Global receipt count at the supplied exact clocks; presence remains unverified |

Missing tables or columns produce `unknown` and null counts. An observed zero
is distinct from an unavailable count. Evidence rows produce
`observed_unverified`. A captured document does not prove extraction. A V2
observation does not prove admission. A publication seal count does not verify
its payload or the full expected population. Canonical cell counts do not prove
consumer parity. No percentage of completion is calculated.

The census uses the existing reporting selector and SQLite whole-second clock
comparisons. Rows within the same second can be included even if their fraction
is later than the supplied cutoff. This limit is explicit in
`evidence_clock_precision`. Population receipt clock equality uses parsed UTC
timestamps with microsecond precision, named in `receipt_clock_precision`.
The census is not suitable as an exact reconstruction or release verifier.

Use `execution/audit_data_cutover_readiness.py` for the authoritative global
13-gate audit. Use `execution/evaluate_population_cutover.py` without `--apply`
for a fresh full-universe cutover dry run. These commands require their own
explicit clock and policy inputs. A small cohort does not replace that universe.

For the first bounded rollout, choose a few issuers with different source paths:
one SEC filer with an acquired native filing package; one issuer whose useful
metrics require an IR earnings release or presentation; and one issuer with an
explicit acquisition or processing gap. Select canonical IDs from existing
identity evidence. Include an issuer with retained source bytes and a clear
document/period scope before testing extraction. These are selection criteria,
not a claim that any live issuer currently qualifies.

Resolve acquisition blockers first. Then populate processing obligations and
sealed dispositions through the existing owners. Populate the source fact and
semantic resolution planes for the agreed documents and periods. Reconcile the
expected fact population and inspect provenance. Run the authoritative audit
and cutover dry run after those stages. Any production population or publication
action needs its existing authorization and exact reviewed candidate.

The census creates no artifact. If retained output is needed, use one named
task artifact under `.tmp/`; apply the existing retention policy. Do not create
another database copy for this report.

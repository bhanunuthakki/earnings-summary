# Windows evidence SOP (non-governing)

These notes describe how to collect evidence for the test-database audit. They
do not change repository policy or replace any canonical directive.

1. On the canonical Windows checkout, record the commit, repository path, and
   command version before collecting evidence.
2. Run the audit against the tracked test sources and export its JSON receipt.
3. Any canonical Windows database evidence must be opened read-only. Use an
   approved provenance-bearing snapshot/export when a snapshot is authorized;
   never create, seed, migrate, or mutate production state for this audit.
4. Transfer only the receipt and its hashes to the review workspace. Record the
   Windows hostname/origin and capture time as evidence metadata.
5. A HOLD, missing receipt, uncertain provenance, or inability to prove
   read-only access is not a pass; stop and escalate to the repository owner.

Receipt template:

```text
approver: <name/role>
capture_date_utc: <YYYY-MM-DD>
snapshot_hash: <sha256>
snapshot_schema: <version>
snapshot_age: <duration>
read_only_proof: <command/output>
provenance: <canonical checkout or approved snapshot/export>
retention: <location and expiry>
receipt_command: .venv\\Scripts\\python.exe execution\\audit_test_db_patterns.py --root <checkout>
```

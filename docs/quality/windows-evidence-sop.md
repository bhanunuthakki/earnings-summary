# Windows evidence SOP (non-governing, source-only collection)

> Non-governing notes. They grant no conversion authority, score admission,
> production evidence, enforcement, or DB-safety claim. Admission stays HOLD.

No database or snapshot is accessed or required for this audit. The scanner
reads only Git-tracked `tests/**/*.py` and `instruction_tests/**/*.py` source
text with a static AST pass. It never imports or executes tests, never opens
a database, and never inspects checkout data beyond tracked source bytes.

## Collection

1. Use the canonical Windows checkout path. Record the exact commit (`HEAD`
   lowercase hex 40/64), repository path, and command version first.
2. Confirm a clean relevant tree: `tests`, `instruction_tests`, and the
   scanner closure (`src/quality/test_db_patterns.py`,
   `src/quality/git_env.py`, `execution/audit_test_db_patterns.py`). Any
   relevant dirty path or Git uncertainty is HOLD; stop and escalate.
3. Run the source-only audit and keep its JSON receipt with provenance:
   `execution\audit_test_db_patterns.py --root <checkout>`. Record
   `scanner_sha256` (ordered closure bytes) and `source_sha256` (ordered
   tracked path plus exact bytes).
4. Transfer only the receipt and hashes. Record hostname/origin and capture
   time (UTC) as metadata. Never copy raw exception text, source snippets,
   environment content, or secret-bearing text.

## Interpretation

- `collection_status` COMPLETE/HOLD describes Git collection integrity.
- `raw_audit_status` PASS/HOLD describes static patterns only. PASS never
  claims DB safety, conversion authority, score admission, production
  evidence, or enforcement.
- `admission_status` is always HOLD with
  `disposition_and_ratchet_deferred`. A HOLD, missing receipt, or uncertain
  provenance is not a pass; escalate to the repository owner.

Receipt template: approver, capture_date_utc, scoped_commit, scanner_sha256,
source_sha256, receipt_command.

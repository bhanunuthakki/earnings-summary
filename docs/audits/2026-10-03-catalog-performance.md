# Workbench catalog performance

The live Workbench field picker took 14.9 seconds at both supported widths.
The catalog endpoint took about 9.6–10.3 seconds on repeated reads. An initial
post-deployment read exceeded its 17-second client deadline.

The KPI catalog repeated canonical resolution in its candidate-definition
query. The Windows query plan started that query from resolved outcomes and
visited the selection ledger before it applied the requested ticker filter.
The revised query materializes only the distinct definition IDs that have a
canonical, admitted requested row. It uses the existing fail-closed admission
predicate. The aggregate and anchor retain their canonical relations. Anchor
history remains complete across issuers for those definitions. No financial
row, source authority, identity rule, or catalog field limit is changed.

Serial read-only comparisons on the canonical Windows database returned equal
complete catalog payloads:

| Requested tickers | Previous query | Revised query | Equal complete payload SHA-256 |
| --- | ---: | ---: | --- |
| NU | 1782.1 ms | 90.0 ms | `04054906a73f5ffc4dcfe9abadb661c32cd1e5380dd05b386426bcda5ac90bfd` |
| NU, META, BN | 6855.9 ms | 1044.2 ms | `ac75e759ed9445c765775704e757ee5827bbeb035d4d4b475459553aef894687` |

These are ordinary serial reads. They are not latency percentile estimates.
The receipt and full query plans are retained locally in
`.tmp/sec-job-recovery/catalog-benchmark-definition.log`. Earlier candidates
that materialized facts were rejected: both complete `SELECT *` rows and
narrow projected rows prevented admission from driving the canonical lookup
and increased measured latency. Their receipts remain retained.

The same endpoint and read-only behavior remain in use. There is no operational
surface change: the catalog adds no operator action, write, cache, migration,
provider job, or background owner. Local regression coverage compares aggregate
rows with the previous query, checks canonical rejection and semantic admission,
and retains full cross-issuer anchor history and catalog grouping/origin rules.

Deployment and live browser verification of this correction are pending.

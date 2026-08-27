# SEC Issuer Identity Reconciliation

**Class:** runbook. Governed by `data_pipeline_dag.md`, `data_provenance.md`, and
`operations_governance_surface.md`.

## Contract

- **Target sources:** SEC `company_tickers.json`, SEC submissions JSON, and an exact
  SEC filing document that explicitly proves a former-to-successor ticker transition.
- **Output schema:** typed issuer, reporting-entity, identifier, authority-surface,
  legacy-ticker binding, recorded-subject binding, and historical-retention revisions;
  exact raw source bytes and their observations are append-only evidence.
- **Refresh cadence:** on demand when the canonical company-ticker bootstrap leaves a
  historical evidence ticker unresolved. This is not a scheduled refresh.
- **Logical Idempotency Key:** former ticker, canonical SEC CIK, successor ticker,
  transition accession, and the two source content identities.
- **Content Identity:** SHA-256 of each exact SEC response body; the resolution uses a
  deterministic commitment over both hashes.
- **Observation Version:** SEC source URL, content identity, and evidence-recorded time.
- **Attempt Identity:** the unique job-lock attempt for the named database and blob root.
- **Rate-limit budget:** at most two SEC requests per invocation, sequential, with the
  configured identifying User-Agent and no retry after a 401 or 403.
- **Failure-mode policy:** missing current successor, a still-current former ticker,
  CIK or accession conflict, absent transition wording/date, non-200 response, schema
  drift, or existing blob mismatch fails before registry mutation. Dry-run is default.

## Procedure

1. Run `execution/bootstrap_issuer_reporting_registry.py` first and retain its receipt.
2. Use `execution/bootstrap_sec_historical_issuer.py` only when SEC submissions contain
   a valid Form 15 reporting termination for the requested historical ticker.
3. If the issuer still reports under a successor ticker, run
   `execution/bootstrap_sec_former_ticker.py` with the former ticker, current successor,
   CIK, exact transition date, current submissions source, and exact SEC transition
   filing. Never substitute a Form 15 disposition for a ticker change.
4. Inspect the dry-run receipt, then repeat with `--apply` under the same authority
   inputs. Replaying the same source identities must create zero additional records.
5. Re-run recorded-subject population and require every affected
   `legacy-ticker:<TICKER>` document subject to select the intended legal registrant.

## Operations-surface disposition

These CLIs are bounded migration/reconciliation tools, not supported recurring operator
workflows. They add no Scheduler task, service, dashboard status, or UI mutation control.
The Operations workspace therefore has **no surface change**; source evidence and typed
database revisions are the durable audit surface.

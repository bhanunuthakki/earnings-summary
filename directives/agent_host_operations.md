# Agent host operations runbook

This runbook supplies mechanics under the production authority in `../AGENTS.md`; it does not
choose a live path or grant execution permission. Read it before host access, service repair,
database snapshot restoration, or resource handoff.

## Mac/Windows listener ownership

- The always-on production-shaped host is Windows: `es-dashboard` owns loopback `127.0.0.1:7421`, and the Portfolio Tracker API owns loopback `127.0.0.1:8000`. The dashboard reaches the tracker on that same Windows host.
- The production database authority is configured outside this repository. The implicit checkout-default `data/portfolio.db` is never a live, fallback, replica, or roster authority and must not exist. Explicit disposable test databases and approved `.tmp/` snapshot restores below are distinct. Treat an implicit checkout-default database as an invalid local artifact: do not inspect it for product facts, migrate it, seed it, or make code pass against it.
- Mac development and tests must name an explicit disposable migrated database under a test/temp root. A Mac task that needs live roster or production facts must coordinate Windows access and use the canonical Windows database read-only or an explicitly approved provenance-bearing snapshot/export (restore via `python cron/restore_db.py --latest --to .tmp/portfolio_local.db` and set `EARNINGS_SUMMARY_DB_PATH=.tmp/portfolio_local.db`). It must never silently create the checkout-default database.
- A Mac browser must open the exact private HTTPS origin printed by live `tailscale serve status` on Windows. Mac `127.0.0.1:7421`, a remembered Windows computer name, a raw Tailnet IP, or the DNS name from `tailscale status` is not a substitute.
- Expose only the dashboard through Tailscale Serve. Keep both backends loopback-only; do not expose port 8000 separately and never use Funnel.
- After a Windows or Tailscale rename, run the documented Serve reset/reapply flow, set `COMMENTS_SERVER_CORS_WHITELIST` to that exact new HTTPS origin, restart `es-dashboard`, then prove Windows-local dashboard/tracker health and Mac-to-Windows dashboard hydration.
- A CRD or browser tab that is auto-reconnecting, including one stuck on Connecting, still owns the GUI session. Before handoff, close or navigate it away, verify that it does not reconnect, and explicitly transfer the database, scheduler, service, and browser resources that remain in scope.

## Clean release alignment and recovery

Keep code identity separate from durable state. A clean release checkout may supply
`upgrade_database.py --repo-root` and readiness `checkout_root`; scratch remains the
configured product-state root. `--runtime-root` must name the actual OS-resolved managed
runtime, and `--db-path` must remain the configured canonical database. A separate code
checkout does not make that database isolated.

1. Preserve and reconcile the existing runtime and source edits before replacement.
   Retain their real Git HEADs, tracked changes, deletions, untracked code, and exact file
   bytes in verified manifests/archives. Reconcile status-only paths as well as Git diff
   output. Review each hotfix against the release, recording whether it is included,
   superseded, or retained outside deployment. Preserve private data, workbooks, credentials,
   environment configuration, virtual environments, and approved state junctions in their
   existing authority; never publish them as release content.
2. Build and validate one reviewed release commit through the normal repository path.
   The clean source checkout and installed runtime must have that same actual commit,
   matching migration graphs, and clean relevant source status. The current readiness
   guard also requires the source checkout at freshly fetched `origin/main`. Verify all
   installed source bytes against the prepared release manifest, including removals and
   unexpected source files; changing HEAD/index metadata or copying a partial patch is
   not deployment proof.
3. Coordinate the sole writer and affected service/scheduler owners before changing runtime
   code. Preserve existing enabled states and operating holds. Account for watchdogs and
   recovery actions so they cannot restart mixed code or restore an old release pin during
   installation. Keep registered actions at the canonical runtime path. Use an explicit
   reviewed code-only installation plan with retained predecessor bytes; do not use
   `git clean`, a hard reset, or a filesystem mirror over state and private files.
4. Before production writes, run the relevant native Windows immutable-file, junction,
   writer-lock, migration, and bounded-processing tests on the actual release bytes and
   managed Python/verified SQLite runtime. Fixtures must use explicit disposable database,
   environment, and secrets roots with provider effects excluded. A skipped Windows-specific
   behavior is not proof. Verify the real runtime and source identities again after installation.
   The dashboard and background ingestion must use the application-owned managed Python
   environment, including reviewed dependency versions; a matching source commit alone is
   insufficient. Manual refresh selects the deployed code root separately from product state.
   Reconcile service interpreter changes with the exact approved-owner baseline and retain
   both prior service configuration and baseline for rollback. Do not update shared global
   Python packages to compensate for an application owner using the wrong environment.
5. Create and validate the database snapshot, manifest, and Phase-0 backup/restore receipt
   under the coordinated writer boundary. Use `create_sqlite_snapshot.py`,
   `backup_restore_readiness_receipt.py`, and the managed SQLite bootstrap with
   `upgrade_database.py --phase0-backup-restore-receipt`; do not bypass readiness with direct
   Alembic or isolated-database flags. For migration 0040, verify retained queue rows,
   history, indexes, and foreign-key integrity before the approved bounded processing batch.
   Inspect its durable receipt and replay before extending processing; do not infer source
   completeness or semantic admission from successful byte capture.
6. Restore only the intended application owners and prior scheduler enabled states, retaining
   existing holds. Verify actual running code, configured state routing, and loopback service
   ownership. A genuine subsequent scheduled run supplies recurrence proof. On failure,
   stop the changed work and restore the retained prior code and matching watchdog pins.
   Preserve appended evidence and immutable files. Database restoration must use the validated
   snapshot and account for intervening legitimate writes; never blindly replace live state
   or force a downgrade that would discard rows requiring the newer schema.

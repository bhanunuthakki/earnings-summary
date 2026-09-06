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

# Portfolio Data Service `/api/v1` fixtures — official (vendored)

All 16 files here are copied **verbatim** from the provider (`portfolio-tracker`)
repo's official synthetic fixture set:

```
%USERPROFILE%\.gemini\antigravity\scratch\portfolio-tracker\docs\api\fixtures\v1\*.json
```

Regenerate them in the provider repo with:

```
python -m portfolio_tracker.api.fixtures_v1
```

then re-copy the output files here — **never hand-edit** these files. If a
value looks wrong or a field is missing, that is a provider-side fixture bug
(or a contract change) to raise there, not something to patch locally.

All values in every file are synthetic (fake tickers `AAAA`/`BBBB`/`CCCC`,
fake account/institution names) — none of this is real portfolio data.

## Provenance

- 9 files (`accounts.json` through `transactions.json` below, minus the 7
  listed as "added 2026-07-24") were vendored first, from the fixture set that
  already existed on `portfolio-tracker` `main`.
- 7 files — `positions.json`, `position-snapshots.json`, `data-quality.json`,
  `performance.json`, `position-performance.json`, `risk.json`,
  `exit-quality.json` — were added 2026-07-24, closing the fixture gap for
  the endpoints that had none. Vendored from `portfolio-tracker`
  `origin/main` @ `451141c` ("feat(api): complete the v1 consumer fixture
  suite", PR #52, merged) via `git show origin/main:docs/api/fixtures/v1/<file>`
  — confirmed byte-identical to the pre-merge `claude/pt-v1-fixtures-completion`
  branch copy first pulled during this same session. Deterministic and
  drift-gated on the provider side (two-run determinism + a drift test, per
  that PR's description).

## Files

| File | Endpoint | Model |
| --- | --- | --- |
| `accounts.json` | `GET /api/v1/accounts` | `AccountsV1Result` |
| `cash-flows.json` | `GET /api/v1/cash-flows` | `CashFlowsV1Result` |
| `health.json` | `GET /api/v1/health` | `HealthV1` |
| `portfolio-snapshot.json` | `GET /api/v1/portfolio-snapshot` | `PortfolioSnapshotV1` |
| `portfolio-snapshot.partial.json` | same, `is_partial=true` variant | `PortfolioSnapshotV1` |
| `portfolio-snapshot.stale.json` | same, `is_stale=true` variant | `PortfolioSnapshotV1` |
| `positioning.json` | `GET /api/v1/analytics/positioning` | `PositioningV1Result` |
| `securities.json` | `GET /api/v1/securities` | `SecuritiesV1Result` |
| `transactions.json` | `GET /api/v1/transactions` | `TransactionsV1Result` |
| `positions.json` (added 2026-07-24) | `GET /api/v1/portfolio/positions` | `PositionsV1Result` (no `meta` — see its docstring) |
| `position-snapshots.json` (added 2026-07-24) | `GET /api/v1/position-snapshots` | `PositionSnapshotsV1Result` |
| `data-quality.json` (added 2026-07-24) | `GET /api/v1/data-quality` | `DataQualityV1Result` (5 findings — good spot-assert material) |
| `performance.json` (added 2026-07-24) | `GET /api/v1/analytics/performance` | `PerformanceV1Result` (365-point series) |
| `position-performance.json` (added 2026-07-24) | `GET /api/v1/analytics/position-performance` | `PositionPerformanceV1Result` |
| `risk.json` (added 2026-07-24) | `GET /api/v1/analytics/risk` | `RiskV1Result` (`{meta, beta, drawdown}` — two top-level result keys, not one nested `result`) |
| `exit-quality.json` (added 2026-07-24) | `GET /api/v1/analytics/exit-quality` | `ExitQualityV1Result` |

Every v1 endpoint now has an official provider fixture. See
`tests/fixtures/tracker_v1/synthetic/README.md` — that directory is now empty
of fixtures and kept only as the fallback location for any future endpoint
that ships without one.

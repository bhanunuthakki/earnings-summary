# Earnings Summary

Earnings Summary is a local-first equity research platform. It collects public
company filings and earnings material, keeps source provenance, and turns that
evidence into reviewable research reports and valuation work.

The core library is under `src/`. Command-line entry points are under
`execution/`, and SQLite schema migrations live in `alembic/`. The project is
designed for one local operator and does not provide a hosted service.

## Quick start

Use Python 3.11 or later and install the project plus development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e '.[dev]'
```

For local development, provide an explicit disposable database path:

```bash
export EARNINGS_SUMMARY_DB_PATH="$(mktemp -d /tmp/earnings-summary-db.XXXXXX)/portfolio.db"
python execution/sqlite_bootstrap.py execution/upgrade_database.py \
  --db-path "$EARNINGS_SUMMARY_DB_PATH" --repo-root . --runtime-root . \
  --allow-isolated-db
python execution/sqlite_bootstrap.py execution/sync_thesis_state.py --apply \
  --db "$EARNINGS_SUMMARY_DB_PATH"
```

Run the checks with `make check-fast`. See [AGENTS.md](AGENTS.md) for the
architecture and [DEFINITIONS.md](DEFINITIONS.md) for domain vocabulary.

## Public boundary

This repository contains source code, migrations, tests, and selected
anonymized research fixtures. It does not contain a portfolio database,
account records, position or cost-basis details, generated reports, scheduler
state, or private runtime snapshots. Those stay in a local checkout and are
ignored by Git. Public research fixtures are not a source of personal
account truth. Tracked UI mockups use synthetic demo figures, not portfolio
records.

Do not commit credentials, OAuth tokens, local databases, downloaded documents,
generated reports, or files containing private holdings or account detail.
The public-tree guard in `execution/verify_public_tree.py` is deterministic and
can be run before sharing a checkout:

```bash
python execution/verify_public_tree.py
```

The application is pull-only and intended for localhost use. Provider access
and external publishing are outside this repository's public contract.

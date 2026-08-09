"""A ratchet on the suite's dominant cost: per-test migration replay.

The chain is 262 migrations. A fixture that stamps a revision and upgrades to
head costs 18-56s, and pytest fixtures are function-scoped by default, so that
price is paid PER TEST rather than per file. Measured on this repo, one such
file's tests ran at ~34s each; after switching to the session-cached template
in ``conftest.migrated_db`` the same tests run at ~4s.

This guard does not demand the backlog be fixed at once — 227 files still build
their own, and some legitimately must (recipes like
``0215_observation_resolution_ledger`` need predecessor tables and fail on a
bare chain build). It only stops the number from GROWING, so new tests reach
for the cached template instead of adding another multi-minute fixture.

To convert a file:

    @pytest.fixture
    def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
        return migrated_db(tmp_path / "x.db", stamp=PRIOR_HEAD)

Then lower ``_MAX_DIRECT_CHAIN_BUILDERS`` by however many you converted.
"""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: Files that still replay migrations in their own fixture. Ratchet DOWN only.
#: Raising this number means a new test just bought the suite another
#: multi-minute fixture — convert it to ``migrated_db`` instead.
_MAX_DIRECT_CHAIN_BUILDERS = 177


def _direct_chain_builders() -> list[str]:
    hits: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file is not this guard's job
            continue
        if "command.upgrade" in text:
            hits.append(path.name)
    return hits


def test_no_new_per_test_migration_replay() -> None:
    builders = _direct_chain_builders()
    assert len(builders) <= _MAX_DIRECT_CHAIN_BUILDERS, (
        f"{len(builders)} test files replay the migration chain in their own "
        f"fixture, up from {_MAX_DIRECT_CHAIN_BUILDERS}. Each one costs 18-56s "
        "PER TEST because pytest fixtures are function-scoped by default. Use "
        "the session-cached template instead:\n\n"
        "    def db_path(tmp_path, migrated_db):\n"
        '        return migrated_db(tmp_path / "x.db", stamp=PRIOR_HEAD)\n\n'
        "If you converted files, lower _MAX_DIRECT_CHAIN_BUILDERS to match."
    )


def test_the_ratchet_is_not_slack() -> None:
    """A ceiling far above reality would pass forever and pin nothing.

    Keeps the allowance within one of the true count, so converting files
    without lowering the bound is caught too.
    """
    actual = len(_direct_chain_builders())
    assert _MAX_DIRECT_CHAIN_BUILDERS - actual <= 1, (
        f"_MAX_DIRECT_CHAIN_BUILDERS is {_MAX_DIRECT_CHAIN_BUILDERS} but only "
        f"{actual} files replay the chain — lower it to {actual} so the ratchet "
        "keeps holding."
    )

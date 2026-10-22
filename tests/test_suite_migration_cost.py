"""Keep direct migration builders visible without depending on call spelling.

This is an inventory ratchet, not a runtime benchmark. Historical one-step
migration tests legitimately invoke upgrade; application fixtures should use
migrated_db. The existing AST scanner resolves imports and aliases; unresolved dynamic upgrade
calls remain visible as candidates rather than disappearing from the inventory.
"""

from __future__ import annotations

import ast
import hashlib
from functools import cache
from pathlib import Path

import pytest

from quality.test_db_invocations import collect_invocations

TESTS_DIR = Path(__file__).resolve().parent
# Resolved and unresolved AST upgrade candidates; old substring count was 109.
# One explicit historical one-step control was added for 0059.
# They use cached predecessor fixtures; application fixtures still use migrated_db.
_MAX_DIRECT_CHAIN_BUILDERS = 102


def _calls_upgrade(source: str, path: str) -> bool:
    invocations = collect_invocations(
        path, ast.parse(source, filename=path), hashlib.sha256(source.encode()).hexdigest()
    )
    return any(call.evidence == "call:upgrade" for call in invocations)


@cache
def _direct_chain_builders() -> tuple[str, ...]:
    hits: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        # All supported import/getattr spellings retain the package name.
        if "alembic" in source and _calls_upgrade(source, str(path)):
            hits.append(path.name)
    return tuple(hits)


def test_no_new_direct_migration_builders() -> None:
    builders = _direct_chain_builders()
    assert len(builders) <= _MAX_DIRECT_CHAIN_BUILDERS, (
        f"{len(builders)} files invoke Alembic upgrade; limit {_MAX_DIRECT_CHAIN_BUILDERS}. "
        "Use the cached migrated_db fixture for current-schema application tests. "
        "For historical migration behavior, keep an explicit historical target."
    )


@pytest.mark.parametrize(
    "name,revision",
    [
        ("test_scoped_research_universe_migration.py", "0059_scoped_research_universe_commitments"),
    ],
)
def test_new_migration_controls_keep_explicit_historical_targets(name: str, revision: str) -> None:
    source = (TESTS_DIR / name).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=name)
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert constants["CURRENT"] == revision
    invocations = collect_invocations(name, tree, hashlib.sha256(source.encode()).hexdigest())
    calls = {
        (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset): node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    upgrades = [item for item in invocations if item.evidence == "call:upgrade"]
    assert upgrades
    for item in upgrades:
        call = calls[
            (
                item.locator.start_line,
                item.locator.start_col,
                item.locator.end_line,
                item.locator.end_col,
            )
        ]
        assert len(call.args) == 2
        assert isinstance(call.args[1], ast.Name) and call.args[1].id == "CURRENT"


def test_the_ratchet_is_not_slack() -> None:
    actual = len(_direct_chain_builders())
    assert _MAX_DIRECT_CHAIN_BUILDERS - actual <= 1, (
        f"lower migration-builder inventory limit from {_MAX_DIRECT_CHAIN_BUILDERS} to {actual}"
    )


@pytest.mark.parametrize(
    "source, expected",
    [
        ("from alembic import command as migrations\nmigrations.upgrade(cfg, 'head')", True),
        ("from alembic.command import upgrade as advance\nadvance(cfg, '0003')", True),
        (
            "from alembic import command\nadvance = getattr(command, 'upgrade')\nadvance(cfg, 'head')",
            True,
        ),
        ("# command.upgrade(cfg, 'head')\ntext = 'alembic.command.upgrade'", False),
    ],
)
def test_upgrade_inventory_resolves_calls_not_comments(source: str, expected: bool) -> None:
    assert _calls_upgrade(source, "tests/test_inventory_fixture.py") is expected

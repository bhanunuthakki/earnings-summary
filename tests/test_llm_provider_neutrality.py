"""Unit Tests: Provider-Neutral Contracts and Adapter Isolation (BHA-56)."""

from __future__ import annotations

from execution.verify_llm_adapter_isolation import main as run_ast_isolation_check


def test_ast_adapter_isolation_passes() -> None:
    exit_code = run_ast_isolation_check()
    assert exit_code == 0

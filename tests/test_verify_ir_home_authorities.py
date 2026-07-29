"""The IR-home CLI selects only reviewed, typed candidates."""

from __future__ import annotations

import pytest

from execution.verify_ir_home_authorities import select_candidates


def test_select_candidates_normalizes_and_preserves_registry_order() -> None:
    selected = select_candidates(("meta", "AMZN"))

    assert [candidate.ticker for candidate in selected] == ["AMZN", "META"]


def test_select_candidates_rejects_unknown_ticker() -> None:
    with pytest.raises(ValueError, match="no reviewed IR-home candidate"):
        select_candidates(("UNKNOWN",))


def test_select_candidates_collapses_ticker_alias_to_one_authority_target() -> None:
    selected = select_candidates(("GOOGL", "GOOG"))

    assert [candidate.ticker for candidate in selected] == ["GOOG"]

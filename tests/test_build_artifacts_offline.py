from __future__ import annotations

from pathlib import Path

from compute import company_description, platform_diagram, segment_definitions, valuation_basis


def test_cache_loaders_do_not_create_directories_on_read(tmp_path: Path) -> None:
    assert company_description.load_description(tmp_path, "NU") is None
    assert platform_diagram.load_diagram(tmp_path, "NU") is None
    assert valuation_basis.load(tmp_path, "NU") is None
    assert segment_definitions.load_definitions(tmp_path, "NU") == {}

    assert not (tmp_path / "data").exists()

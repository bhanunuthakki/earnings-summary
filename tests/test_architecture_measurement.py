from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quality import architecture as analyzer
from quality.architecture import (
    ArchitectureMetrics,
    ArchitectureRatchetReceipt,
    ArchitectureReceipt,
    analyze_sources,
    architecture_regressions,
    compare_architecture,
)


def _sources() -> dict[str, str]:
    return {
        "src/a.py": "from b import helper\n\ndef public(value: int) -> int:\n    return helper(value)\n",
        "src/b.py": "from a import public\n\ndef helper(value: int) -> int:\n    return value + 1\n",
        "execution/comments_server.py": "def main() -> None:\n    pass\n",
        "src/pipeline/portfolio_panel.py": "def render() -> str:\n    return 'ok'\n",
    }


def _receipt(
    metrics: ArchitectureMetrics | None = None, commit: str = "abc"
) -> ArchitectureReceipt:
    return ArchitectureReceipt(
        scoped_revision="WORKTREE",
        scoped_commit=commit,
        scanner_sha256="0" * 64,
        source_sha256="1" * 64,
        python_version="3.11",
        ast_version="python-3.11",
        definitions={"x": "y"},
        metrics=metrics or analyze_sources(_sources()),
    )


def test_architecture_receipt_schema_is_explicit() -> None:
    receipt = _receipt()
    assert receipt.schema_version == "architecture-measurement-v1"


def test_architecture_scan_is_deterministic_and_resolves_cycle() -> None:
    first = analyze_sources(_sources())
    second = analyze_sources(dict(reversed(tuple(_sources().items()))))
    assert first == second
    assert first.strongly_connected_components == (("a", "b"),)
    assert first.scc_count == 1
    assert first.largest_scc == 2


def test_noncomment_loc_excludes_blank_and_comment_only_lines() -> None:
    sources = _sources()
    sources["src/a.py"] = "# comment\n\nvalue = 1  # inline\n"
    metrics = analyze_sources(sources)
    module = next(item for item in metrics.modules if item.path == "src/a.py")
    assert module.lines.physical == 3
    assert module.lines.nonblank == 2
    assert module.lines.noncomment == 1


def test_tracked_paths_preserve_non_ascii_and_whitespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = "src/café module.py\0execution/plain.py\0docs/ignored.py\0".encode()

    def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(analyzer.subprocess, "run", fake_run)

    assert analyzer.tracked_python_paths(tmp_path) == (
        "execution/plain.py",
        "src/café module.py",
    )


def test_architecture_ratchet_rejects_metric_growth() -> None:
    baseline = analyze_sources(_sources())
    current_sources = _sources()
    current_sources["src/large.py"] = "\n".join(f"x_{index} = {index}" for index in range(1002))
    current = analyze_sources(current_sources)
    regressions = architecture_regressions(current, baseline)
    assert any(item.startswith("total_noncomment_loc increased") for item in regressions)
    assert any(item.startswith("modules_over_1000_loc increased") for item in regressions)


def test_architecture_ratchet_detects_scc_member_replacement_without_order_noise() -> None:
    baseline = analyze_sources(_sources())
    reordered = baseline.model_copy(
        update={
            "strongly_connected_components": (("b", "a"),),
            "composition_root_loc": dict(baseline.composition_root_loc),
        }
    )
    assert architecture_regressions(reordered, baseline) == ()

    replacement = baseline.model_copy(update={"strongly_connected_components": (("a", "c"),)})
    regressions = architecture_regressions(replacement, baseline)
    assert "scc member set introduced: (a, c)" in regressions


def test_architecture_ratchet_catches_same_aggregate_scc_substitution() -> None:
    baseline = analyze_sources(_sources())
    replacement = baseline.model_copy(update={"strongly_connected_components": (("a", "c"),)})

    assert replacement.scc_count == baseline.scc_count
    assert replacement.scc_module_count == baseline.scc_module_count
    assert replacement.largest_scc == baseline.largest_scc
    assert "scc member set introduced: (a, c)" in architecture_regressions(replacement, baseline)


def test_architecture_ratchet_allows_scc_removal_and_strict_subset() -> None:
    baseline = analyze_sources(_sources()).model_copy(
        update={"strongly_connected_components": (("a", "b", "c"),)}
    )
    subset = baseline.model_copy(
        update={"strongly_connected_components": (("a", "b"),), "scc_module_count": 2}
    )
    assert architecture_regressions(subset, baseline) == ()

    removed = baseline.model_copy(
        update={"strongly_connected_components": (), "scc_count": 0, "scc_module_count": 0}
    )
    assert architecture_regressions(removed, baseline) == ()


def test_architecture_ratchet_compares_each_composition_root_loc() -> None:
    baseline = analyze_sources(_sources())
    unchanged = baseline.model_copy(
        update={"composition_root_loc": dict(baseline.composition_root_loc)}
    )
    assert architecture_regressions(unchanged, baseline) == ()

    grown = baseline.model_copy(
        update={
            "composition_root_loc": {
                **baseline.composition_root_loc,
                "execution/comments_server.py": baseline.composition_root_loc[
                    "execution/comments_server.py"
                ]
                + 1,
            }
        }
    )
    assert (
        "composition_root_loc[execution/comments_server.py] increased from "
        f"{baseline.composition_root_loc['execution/comments_server.py']} to "
        f"{baseline.composition_root_loc['execution/comments_server.py'] + 1}"
        in architecture_regressions(grown, baseline)
    )

    unavailable = baseline.model_copy(
        update={"composition_root_loc": {"execution/comments_server.py": -1}}
    )
    assert not any(
        item.startswith("composition_root_loc[")
        for item in architecture_regressions(unavailable, baseline)
    )


def test_architecture_ratchet_receipt_is_small_and_typed() -> None:
    receipt = ArchitectureRatchetReceipt(
        schema_version="architecture-ratchet-v1",
        status="PASS",
        scoped_commit="new",
        baseline_commit="old",
        current_source_sha256="a",
        baseline_source_sha256="b",
        regressions=(),
        scanner_mismatch=False,
    )
    assert receipt.model_dump()["status"] == "PASS"


def test_architecture_comparison_fails_closed_on_scanner_drift() -> None:
    baseline = _receipt(commit="baseline")
    current = _receipt(commit="current").model_copy(update={"scanner_sha256": "2" * 64})

    result = compare_architecture(current, baseline)

    assert result.status == "HOLD"
    assert result.scanner_mismatch is True


def test_no_checkout_database_is_needed(tmp_path: Path) -> None:
    metrics = analyze_sources(_sources())
    assert metrics.executable_modules == 4
    assert not list(tmp_path.glob("*.db"))

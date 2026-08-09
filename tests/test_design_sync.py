"""Regression tests for the repository-level design-sync gate."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from scripts import check_design_sync


def test_control_parity_rejects_matching_selectors_with_different_rules(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    react = tmp_path / "controls.css"
    react.write_text(
        "\n".join(
            (
                ".k-sidebar { width: 1px; }",
                ".k-icon { width: 1px; }",
                ".k-icon-btn { width: 1px; }",
                ".k-nav-item { width: 1px; }",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_design_sync, "REACT_CONTROLS", react)

    failures = check_design_sync.control_parity_failures()

    assert failures
    assert all("declarations drifted" in failure for failure in failures)


def test_design_sync_gate_is_wired_into_hosted_ci() -> None:
    workflow = (check_design_sync.PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    design_job = workflow.split("\n  design:\n", 1)[1].split("\n  quality:\n", 1)[0]
    assert "pip install --require-hashes -r requirements.lock" in design_job
    assert "python scripts/check_design_sync.py" in design_job


def test_geometry_baseline_rejects_growth_and_requires_shrink_updates() -> None:
    assert check_design_sync.geometry_baseline_failures(
        actual={"src/example.py": 3}, baseline={"src/example.py": 2}
    ) == ["src/example.py: raw geometry grew 2 -> 3"]
    assert check_design_sync.geometry_baseline_failures(
        actual={"src/example.py": 1}, baseline={"src/example.py": 2}
    ) == ["src/example.py: raw geometry shrank 2 -> 1; lower the checked-in baseline"]
    assert check_design_sync.geometry_baseline_failures(
        actual={"src/new_surface.py": 1}, baseline={}
    ) == ["src/new_surface.py: raw geometry grew 0 -> 1"]


def test_work_os_contract_checks_the_runtime_prototype_and_mobile_rail() -> None:
    assert check_design_sync.work_os_contract_failures() == []

    mockup = check_design_sync.WORK_OS_PROTOTYPE.read_text(encoding="utf-8")
    drifted = mockup.replace("--fs-display: 20px", "--fs-display: 21px", 1)
    failures = check_design_sync.work_os_contract_failures(mockup_text=drifted)
    assert failures == ["Work OS token --fs-display is '21px'; expected '20px'"]

    renderer = check_design_sync.WORK_OS_RENDERER.read_text(encoding="utf-8")
    regressed = renderer.replace(
        "font-size: var(--mobile-control-font-size) !important",
        "font-size: var(--fs-title) !important",
        1,
    )
    failures = check_design_sync.work_os_contract_failures(renderer_source=regressed)
    assert "Work OS renderer missing production design contract " in failures[-1]
    assert "mobile-control-font-size" in failures[-1]

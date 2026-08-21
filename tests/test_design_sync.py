"""Regression tests for the repository-level design-sync gate."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from pytest import MonkeyPatch

from scripts import check_design_sync, gen_design_conformance_debt, gen_design_controls


def test_generated_controls_are_exact_canonical_output_plus_react_extensions() -> None:
    rendered = gen_design_controls.render_css()
    canonical = gen_design_controls.canonical_css()

    assert canonical == gen_design_controls.canonical_region(rendered)
    assert gen_design_controls.REACT_EXTENSIONS_START in rendered
    assert gen_design_controls.REACT_EXTENSIONS in rendered


def test_generated_controls_check_rejects_omitted_or_changed_canonical_rules(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controls = tmp_path / "controls.css"
    rendered = gen_design_controls.render_css()
    controls.write_text(rendered.replace(".k-btn {", ".k-button {", 1), encoding="utf-8")
    monkeypatch.setattr(gen_design_controls, "CSS_OUTPUT", controls)

    assert not gen_design_controls.check()

    controls.write_text(
        rendered.replace(".k-overlay {", ".k-overlay { border: 99px solid red;", 1),
        encoding="utf-8",
    )
    assert not gen_design_controls.check()


def test_generated_controls_cover_button_chip_overlay_and_grid_primitives() -> None:
    canonical = gen_design_controls.canonical_css()
    for selector in (".k-btn {", ".k-chip {", ".k-overlay {", ".k-grid-shell,"):
        assert selector in canonical


def test_react_extensions_delegate_geometry_to_master_tokens() -> None:
    """React composites may add selectors, but not a second geometry scale."""

    extensions = gen_design_controls.REACT_EXTENSIONS
    assert re.search(r"var\(--(?:sp|icon|bw|grid|z)-", extensions)
    assert not re.search(r"(?<![\w.-])\d+(?:\.\d+)?px\b", extensions)
    assert not re.search(r"(?<![\w.-])\d+(?:\.\d+)?em\b", extensions)
    assert not re.search(r"--[a-z][\w-]*\s*:", extensions)


def test_react_component_props_have_no_open_visual_override_apis() -> None:
    """Component consumers select kit variants/classes, never inline geometry."""

    component_root = Path(__file__).parents[1] / "design-system" / "src" / "components"
    source = "\n".join(path.read_text(encoding="utf-8") for path in component_root.glob("*.tsx"))
    assert not re.search(r"\bstyle\??\s*:\s*(?:React\.)?CSSProperties", source)
    assert not re.search(r"\sstyle=\{", source)
    assert "nameMax" not in source
    assert not re.search(r"\bsize\??\s*:\s*number", source)


def test_react_entrypoint_uses_declarative_element_output() -> None:
    entrypoint = (Path(__file__).parents[1] / "design-system" / "src" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert "createElement(" not in entrypoint
    assert 'from "react/jsx-runtime"' in entrypoint


def test_design_sync_reports_control_generator_drift(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(check_design_sync, "check_generated_controls", lambda: False)

    assert check_design_sync.generated_control_failures() == [
        "generated React controls drifted from src/ui/controls.py"
    ]


def test_design_sync_delegates_to_clean_canonical_conformance_receipt() -> None:
    assert check_design_sync.conformance_receipt_failures() == []


def test_exact_design_debt_ledger_is_current_and_rejects_identity_growth(
    tmp_path: Path,
) -> None:
    assert gen_design_conformance_debt.check()
    payload = gen_design_conformance_debt.build_payload()
    assert isinstance(payload["geometry"], list)
    assert payload["geometry"] == []
    assert payload["findings"] == []
    assert payload["unverifiable"] == []
    payload["geometry"] = ["ui/new.py|root|.x|padding|17px|#1"]
    drifted = tmp_path / "design_conformance_debt.json"
    drifted.write_text(gen_design_conformance_debt.canonical_json(payload), encoding="utf-8")
    assert not gen_design_conformance_debt.check(baseline=drifted)

    current = gen_design_conformance_debt.build_payload()
    assert isinstance(current["findings"], list)
    candidate = dict(current)
    candidate["findings"] = [*current["findings"], "ui/new.py|color|red"]
    assert gen_design_conformance_debt.growth_failures(candidate, current) == [
        "findings debt grew by 1 exact identities"
    ]


def test_merge_base_gate_rejects_hand_edited_debt_growth(monkeypatch: MonkeyPatch) -> None:
    head = gen_design_conformance_debt.build_payload()
    base = dict(head)
    findings = head["findings"]
    assert isinstance(findings, list)
    assert all(isinstance(item, str) for item in cast(list[object], findings))
    typed_findings = cast(list[str], findings)
    base["findings"] = list(typed_findings)
    candidate = dict(head)
    candidate["findings"] = [*typed_findings, "ui/new.py|color|red"]

    def ledger_at_merge_base(_project_root: Path, _base_ref: str) -> dict[str, object]:
        return base

    monkeypatch.setattr(gen_design_conformance_debt, "_ledger_at_merge_base", ledger_at_merge_base)

    assert gen_design_conformance_debt.merge_base_growth_failures(
        "origin/main", candidate=candidate
    ) == ["findings debt grew by 1 exact identities"]


def test_merge_base_gate_allows_only_explicit_first_ledger_bootstrap(
    monkeypatch: MonkeyPatch,
) -> None:
    def missing_ledger(_project_root: Path, _base_ref: str) -> None:
        return None

    monkeypatch.setattr(gen_design_conformance_debt, "_ledger_at_merge_base", missing_ledger)
    assert gen_design_conformance_debt.merge_base_growth_failures("origin/main") == [
        "merge-base design debt ledger is missing"
    ]
    assert (
        gen_design_conformance_debt.merge_base_growth_failures(
            "origin/main", allow_missing_base=True
        )
        == []
    )
    debt = gen_design_conformance_debt.build_payload()
    debt["geometry"] = ["ui/new.py|root|.x|padding|17px|#1"]
    assert gen_design_conformance_debt.merge_base_growth_failures(
        "origin/main", candidate=debt, allow_missing_base=True
    ) == ["first-ledger bootstrap must be debt-free: geometry debt grew by 1 exact identities"]


def test_design_sync_gate_is_wired_into_hosted_ci() -> None:
    workflow = (check_design_sync.PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    test_job = workflow.split("\n  tests:\n", 1)[1].split("\n  design:\n", 1)[0]
    design_job = workflow.split("\n  design:\n", 1)[1].split("\n  quality:\n", 1)[0]
    assert "grep -v '^tests/test_design_computed_canary.py$'" in test_job
    assert "pip install --require-hashes -r requirements.lock" in design_job
    assert "pip install -e .[dev]" in design_job
    assert "Build verified SQLite writer runtime" in design_job
    assert 'echo "LD_PRELOAD=$sqlite_dir/libsqlite3.so.0" >> "$GITHUB_ENV"' in design_job
    assert "python scripts/check_design_sync.py" in design_job
    assert "python scripts/gen_design_conformance_debt.py --base-ref" in design_job
    assert "--allow-missing-base" in design_job
    assert "fetch-depth: 0" in design_job
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" in design_job
    assert "npm ci" in design_job
    assert "npm run check" in design_job


def test_work_os_contract_checks_the_runtime_prototype_and_mobile_rail() -> None:
    assert check_design_sync.work_os_contract_failures() == []

    mockup = check_design_sync.WORK_OS_PROTOTYPE.read_text(encoding="utf-8")
    drifted = mockup.replace("--fs-display: 20px", "--fs-display: 21px", 1)
    failures = check_design_sync.work_os_contract_failures(mockup_text=drifted)
    assert failures == ["Work OS token --fs-display is '21px'; expected '20px'"]

    renderer = (
        check_design_sync.WORK_OS_RENDERER.read_text(encoding="utf-8")
        + "\n"
        + check_design_sync.WORK_OS_STYLE_MASTER.read_text(encoding="utf-8")
    )
    regressed = renderer.replace(
        "font-size: var(--mobile-control-font-size) !important",
        "font-size: var(--fs-title) !important",
        1,
    )
    failures = check_design_sync.work_os_contract_failures(renderer_source=regressed)
    assert "Work OS renderer missing production design contract " in failures[-1]
    assert "mobile-control-font-size" in failures[-1]

    regressed = renderer.replace("k-card-row-title", "stat-heading", 1)
    failures = check_design_sync.work_os_contract_failures(renderer_source=regressed)
    assert any("hydrated Action Queue" in failure for failure in failures)


def test_work_os_contract_accepts_composed_classes_and_rejects_substrings() -> None:
    assert check_design_sync.has_html_class(
        '<h3 class="k-card-title k-card-row-title">Queue</h3>', "k-card-row-title"
    )
    assert check_design_sync.has_html_class(
        "<h3 class='k-card-row-title k-card-title'>Queue</h3>", "k-card-row-title"
    )
    assert not check_design_sync.has_html_class(
        '<h3 class="not-k-card-row-title">Queue</h3>', "k-card-row-title"
    )
    assert not check_design_sync.has_html_class(
        '<h3 data-class="k-card-row-title">Queue</h3>', "k-card-row-title"
    )

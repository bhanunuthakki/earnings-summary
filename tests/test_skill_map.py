"""Hermetic tests for generated skill map rendering and structural checks (BHA-165)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import cast

import yaml

from scripts import check_investing_skill

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_REL = Path("src/advisor/skills")


def _stage_clean_skill_repo(dst: Path) -> None:
    """Copy tracked skill definitions + minimal referenced repo files into tmp_path."""
    shutil.copytree(REPO_ROOT / SKILLS_REL, dst / SKILLS_REL)
    # Create minimal empty stubs for any repo file entrypoint checked via _safe_path.
    for yaml_file in (dst / SKILLS_REL).glob("*/agents/openai.yaml"):
        loaded_raw = cast(object, yaml.safe_load(yaml_file.read_text(encoding="utf-8")))
        if not isinstance(loaded_raw, dict):
            continue
        loaded = cast("dict[object, object]", loaded_raw)
        routing_raw = loaded.get("routing", {})
        if not isinstance(routing_raw, dict):
            continue
        routing = cast("dict[object, object]", routing_raw)
        entrypoints_raw = routing.get("entrypoints", [])
        if not isinstance(entrypoints_raw, list):
            continue
        for entry in cast("list[object]", entrypoints_raw):
            if isinstance(entry, str) and entry.startswith(
                ("directives/", "execution/", "src/", "scripts/")
            ):
                target = dst / entry
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_text("# test stub\n", encoding="utf-8")


def _add_specialist_skill(
    root: Path,
    slug: str,
    *,
    to_target: str = "earnings-summary-investing",
    approval: str = "Owner request required before full evaluation",
    entrypoint: str = "execution/review_position.py",
) -> None:
    skill_dir = root / SKILLS_REL / slug
    (skill_dir / "agents").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: Synthetic specialist {slug}\n---\n\n# {slug}\n",
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "interface": {
            "display_name": slug.replace("-", " ").title(),
            "short_description": "Synthetic test skill",
            "default_prompt": f"Use ${slug}.",
        },
        "policy": {"allow_implicit_invocation": True},
        "routing": {
            "role": "specialist",
            "trigger": f"Trigger for {slug}",
            "inputs": ["Explicit input"],
            "outputs": ["Explicit output"],
            "entrypoints": [entrypoint],
            "handoffs": [
                {
                    "to": to_target,
                    "condition": "Qualified finding",
                    "approval": approval,
                }
            ],
        },
    }
    (skill_dir / "agents" / "openai.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def test_real_repo_skill_map_is_clean_and_renders_all_skills_and_handoffs(
    capsys: object,
) -> None:
    del capsys
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(REPO_ROOT)]) == 0
    md = check_investing_skill.render_skill_map(REPO_ROOT, fmt="markdown")
    mermaid = check_investing_skill.render_skill_map(REPO_ROOT, fmt="mermaid")
    text_tree = check_investing_skill.render_skill_map(REPO_ROOT, fmt="text")

    for slug in (
        "earnings-summary-investing",
        "next-dollar-allocation",
        "ownership-disclosure-scan",
    ):
        assert slug in md
        assert slug in mermaid
        assert slug in text_tree
    assert "flowchart TD" in mermaid
    assert "Approval:" in mermaid
    assert "Owner approval required" in text_tree
    assert (REPO_ROOT / check_investing_skill.SKILL_MAP_PATH).read_text(encoding="utf-8") == md


def test_one_command_render_skill_map_writes_deterministically(tmp_path: Path) -> None:
    _stage_clean_skill_repo(tmp_path)
    out = tmp_path / check_investing_skill.SKILL_MAP_PATH
    out.unlink()

    rc = check_investing_skill.main(
        [
            "render-skill-map",
            "--repo-root",
            str(tmp_path),
            "--write",
            check_investing_skill.SKILL_MAP_PATH,
        ]
    )
    assert rc == 0
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(tmp_path)]) == 0


def test_fails_when_skill_added_without_metadata_or_route(tmp_path: Path) -> None:
    _stage_clean_skill_repo(tmp_path)
    _add_specialist_skill(tmp_path, "deep-value-screen")

    report = check_investing_skill.check_skill_map(tmp_path)
    assert report["status"] == "invalid"
    assert isinstance(report["errors"], list)
    errors = "\n".join(str(e) for e in cast("list[object]", report["errors"]))
    assert "no entry route" in errors or "missing handoff edge" in errors
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(tmp_path)]) == 2


def test_fails_when_skill_added_without_routing_metadata_file(tmp_path: Path) -> None:
    _stage_clean_skill_repo(tmp_path)
    bare = tmp_path / SKILLS_REL / "bare-skill"
    bare.mkdir(parents=True)
    (bare / "SKILL.md").write_text("---\nname: bare-skill\ndescription: x\n---\n", encoding="utf-8")

    report = check_investing_skill.check_skill_map(tmp_path)
    assert report["status"] == "invalid"
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(tmp_path)]) == 2


def test_fails_when_route_added_without_matching_node_or_edge(tmp_path: Path) -> None:
    _stage_clean_skill_repo(tmp_path)
    routes = tmp_path / check_investing_skill.ROUTES_PATH
    routes.write_text(
        routes.read_text(encoding="utf-8")
        + "\n| Synthetic route | `src/advisor/skills/ghost-skill/SKILL.md` | None |\n",
        encoding="utf-8",
    )

    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(tmp_path)]) == 2

    # Adding the node without adding the router -> specialist edge also fails.
    (tmp_path / SKILLS_REL / "ghost-skill" / "agents").mkdir(parents=True, exist_ok=True)
    _add_specialist_skill(tmp_path, "ghost-skill")
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(tmp_path)]) == 2


def test_fails_on_orphan_handoff_missing_approval_missing_entrypoint_and_stale_map(
    tmp_path: Path,
) -> None:
    # 1. Orphan handoff target
    repo1 = tmp_path / "orphan"
    _stage_clean_skill_repo(repo1)
    _add_specialist_skill(repo1, "screen-a", to_target="nonexistent-target")
    routes1 = repo1 / check_investing_skill.ROUTES_PATH
    routes1.write_text(
        routes1.read_text(encoding="utf-8")
        + "\n| Screen A | `src/advisor/skills/screen-a/SKILL.md` | Run screen |\n",
        encoding="utf-8",
    )
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(repo1)]) == 2

    # 2. Missing approval
    repo2 = tmp_path / "no_approval"
    _stage_clean_skill_repo(repo2)
    _add_specialist_skill(repo2, "screen-b", approval="   ")
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(repo2)]) == 2

    # 3. Missing file entrypoint
    repo3 = tmp_path / "missing_entry"
    _stage_clean_skill_repo(repo3)
    _add_specialist_skill(repo3, "screen-c", entrypoint="execution/does_not_exist.py")
    assert check_investing_skill.main(["check-skill-map", "--repo-root", str(repo3)]) == 2

    # 4. Stale hand-edited skill-map.md
    repo4 = tmp_path / "stale_map"
    _stage_clean_skill_repo(repo4)
    map_file = repo4 / check_investing_skill.SKILL_MAP_PATH
    map_file.write_text("# Outdated manual edit\n", encoding="utf-8")
    report4 = check_investing_skill.check_skill_map(repo4)
    assert report4["status"] == "invalid"
    assert json.loads(json.dumps(report4))["errors"] == [
        f"generated skill map out of date: {check_investing_skill.SKILL_MAP_PATH}"
    ]


def test_check_baseline_reports_broken_skill_map_and_clean_generated_refs(tmp_path: Path) -> None:
    # Real repo check() must have zero errors mentioning skill-map.md or routes.md or skill graph.
    real_report, _ = check_investing_skill.check(REPO_ROOT)
    assert isinstance(real_report["errors"], list)
    real_errors = [str(e) for e in cast("list[object]", real_report["errors"])]
    assert not any(
        any(tok in err for tok in ("skill-map.md", "routes.md", "ownership-disclosure-scan"))
        for err in real_errors
    )

    # A broken skill map turns check() status invalid.
    _stage_clean_skill_repo(tmp_path)
    _add_specialist_skill(tmp_path, "unrouted-screen")
    report, _ = check_investing_skill.check(tmp_path)
    assert report["status"] == "invalid"
    assert isinstance(report["errors"], list)
    assert any("unrouted-screen" in str(e) for e in cast("list[object]", report["errors"]))

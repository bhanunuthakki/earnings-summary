"""Check reviewed skill sources without database, network, or model access.

Exit codes: 0 clean, 1 source drift needing semantic review, 2 invalid input or
broken/unsafe closure. ``record-review`` changes only the reviewed hash baseline;
it requires a review note and each changed path explicitly named by the reviewer.
It never updates skill prose, acquires sources, or approves application policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import yaml

SKILL = "src/advisor/skills/earnings-summary-investing"
SKILLS_DIR = "src/advisor/skills"
ROUTES_PATH = f"{SKILL}/references/routes.md"
SKILL_MAP_PATH = f"{SKILL}/references/skill-map.md"
BASELINE = f"{SKILL}/references/reviewed-sources.json"
_LINK = re.compile(r"\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)")
_SKILL_PATH_RX = re.compile(r"^src/advisor/skills/([a-z0-9-]+)/SKILL\.md$")
_CODE = re.compile(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.DOTALL)
_ROOT_FILES = {"AGENTS.md", "DEFINITIONS.md", "reconstruction_manifest.json"}
_SOURCE_DIRS = {"directives", "execution", "src", "scripts"}
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class SkillHandoff:
    """Directed handoff between two skills and its required owner approval."""

    source: str
    target: str
    condition: str
    approval: str


@dataclass(frozen=True)
class SkillNode:
    """Validated metadata node for one skill under src/advisor/skills/."""

    slug: str
    display_name: str
    role: str
    trigger: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    entrypoints: tuple[str, ...]
    handoffs: tuple[SkillHandoff, ...]


def _without_fenced_blocks(text: str) -> str:
    """Keep prose references separate from fenced illustrative examples."""
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line.rstrip("\r\n"))
        if fence is not None:
            if (
                match
                and match[1][0] == fence[0]
                and len(match[1]) >= len(fence)
                and not match[2].strip()
            ):
                fence = None
            lines.append("\n")
        elif match and (match[1][0] != "`" or "`" not in match[2]):
            fence = match[1]
            lines.append("\n")
        else:
            lines.append(line)
    return "".join(lines)


def _safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or path.is_absolute()
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
        or path.as_posix() != relative
    ):
        raise ValueError("unsafe repository path")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("path resolves outside repository")
    return resolved


def _public_source(root: Path, relative: str) -> Path:
    candidate = _safe_path(root, relative)
    if not candidate.is_file():
        raise ValueError("source must be an existing file")
    for name in (relative, candidate.relative_to(root).as_posix()):
        path = PurePosixPath(name)
        if (
            any(part.startswith(".") for part in path.parts)
            or (name not in _ROOT_FILES and path.parts[0] not in _SOURCE_DIRS)
            or path.suffix not in {".py", ".md", ".json", ".js"}
        ):
            raise ValueError("source name and target must be supported public source paths")
    return candidate


def _load_sources(baseline: Path) -> tuple[dict[str, object], dict[str, str]]:
    raw: object = json.loads(baseline.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("baseline must be an object")
    payload = cast(dict[str, object], raw)
    if set(payload) != {"schema_version", "review_note", "sources"}:
        raise ValueError("baseline fields do not match schema")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported baseline schema")
    note = payload["review_note"]
    entries = payload["sources"]
    if (
        not isinstance(note, str)
        or not note.strip()
        or not isinstance(entries, list)
        or not entries
    ):
        raise ValueError("baseline requires review note and sources")
    sources: dict[str, str] = {}
    for entry_raw in cast(list[object], entries):
        if not isinstance(entry_raw, dict):
            raise ValueError("source entry must be an object")
        entry = cast(dict[str, object], entry_raw)
        if set(entry) != {"path", "sha256"}:
            raise ValueError("source entry fields do not match schema")
        relative, digest = entry["path"], entry["sha256"]
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ValueError("source path and hash must be strings")
        if relative in sources or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("duplicate source or invalid SHA-256")
        parts = PurePosixPath(relative).parts
        if not parts or (relative not in _ROOT_FILES and parts[0] not in _SOURCE_DIRS):
            raise ValueError("source is outside public procedure/code scope")
        if PurePosixPath(relative).suffix not in {".py", ".md", ".json", ".js"}:
            raise ValueError("unsupported reviewed source type")
        sources[relative] = digest
    return payload, sources


def check(
    root: Path,
    baseline_relative: str = BASELINE,
    replacement_sources: dict[str, str] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return a deterministic report and validated baseline; never write."""
    root = root.resolve()
    errors: list[str] = []
    changed: list[dict[str, str]] = []
    skill_markdown_sha256: dict[str, str] = {}
    payload: dict[str, object] = {}
    try:
        baseline = _safe_path(root, baseline_relative)
        payload, sources = _load_sources(baseline)
        if replacement_sources is not None:
            sources = replacement_sources
    except (ValueError, OSError, UnicodeError):
        return {
            "status": "invalid",
            "errors": ["invalid or unavailable reviewed-source baseline"],
            "changed_sources": [],
        }, payload
    for relative, expected in sorted(sources.items()):
        try:
            source = _public_source(root, relative)
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != expected:
                changed.append(
                    {"path": relative, "reviewed_sha256": expected, "current_sha256": actual}
                )
        except (ValueError, OSError):
            errors.append(f"missing, unsafe or unreadable source: {relative}")
    skill = root / SKILL
    for relative in ("SKILL.md", "references/routes.md"):
        if not (skill / relative).is_file():
            errors.append(f"missing skill file: {relative}")
    for file in sorted(skill.rglob("*.md")):
        try:
            if not file.resolve().is_relative_to(root):
                raise ValueError("skill file escapes repository")
            raw = file.read_bytes()
            skill_markdown_sha256[file.relative_to(root).as_posix()] = hashlib.sha256(
                raw
            ).hexdigest()
            text = _without_fenced_blocks(raw.decode("utf-8"))
            for span in _CODE.finditer(text):
                words = span[2].split()
                if not words:
                    continue
                relative = words[0]
                if relative.endswith("/") or "<" in relative:
                    continue
                if relative not in _ROOT_FILES and not relative.startswith(
                    tuple(f"{directory}/" for directory in _SOURCE_DIRS)
                ):
                    continue
                source = _safe_path(root, relative)
                if not source.is_file():
                    errors.append(f"missing skill source reference: {relative}")
                elif relative not in sources and relative != baseline_relative:
                    errors.append(f"unreviewed skill source reference: {relative}")
            for match in _LINK.finditer(text):
                target = match.group(1).strip("<>")
                if target.startswith(("https://", "http://", "mailto:", "#")):
                    continue
                target = target.split("#", 1)[0]
                resolved = (file.parent / target).resolve()
                if (
                    Path(target).is_absolute()
                    or "\\" in target
                    or ":" in target
                    or not resolved.is_relative_to(root)
                    or not resolved.is_file()
                ):
                    errors.append(
                        f"broken or unsafe skill link in {file.relative_to(root).as_posix()}: {target}"
                    )
        except (ValueError, OSError, UnicodeError):
            errors.append("unsafe or unreadable skill Markdown file")
    if (root / SKILLS_DIR).is_dir():
        _nodes, _routes, map_errors = load_skill_graph(root)
        errors.extend(map_errors)
    status = "invalid" if errors else "drift" if changed else "clean"
    return {
        "status": status,
        "errors": errors,
        "changed_sources": changed,
        "skill_markdown_sha256": skill_markdown_sha256,
    }, payload


def _load_yaml_dict(path: Path) -> dict[str, object]:
    raw = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must parse as a YAML mapping")
    return {str(k): v for k, v in cast(dict[object, object], raw).items()}


def _nonempty_str(mapping: dict[str, object], key: str, ctx: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ctx} missing non-empty string {key!r}")
    return value.strip()


def _string_tuple(mapping: dict[str, object], key: str, ctx: str) -> tuple[str, ...]:
    raw = mapping.get(key)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{ctx} missing non-empty list {key!r}")
    items: list[str] = []
    for entry in cast(list[object], raw):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{ctx}.{key} entries must be non-empty strings")
        items.append(entry.strip())
    return tuple(items)


def _is_repo_file_entrypoint(item: str) -> bool:
    return item in _ROOT_FILES or item.startswith(tuple(f"{d}/" for d in _SOURCE_DIRS))


def _parse_skill_frontmatter(skill_md: Path, slug: str) -> None:
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise ValueError(f"{slug}/SKILL.md missing YAML frontmatter")
    end_idx = next((idx for idx in range(1, len(lines)) if lines[idx].strip() == "---"), None)
    if end_idx is None:
        raise ValueError(f"{slug}/SKILL.md unterminated YAML frontmatter")
    raw = cast(object, yaml.safe_load("\n".join(lines[1:end_idx])))
    if not isinstance(raw, dict):
        raise ValueError(f"{slug}/SKILL.md frontmatter must be a mapping")
    fm = {str(k): v for k, v in cast(dict[object, object], raw).items()}
    name = _nonempty_str(fm, "name", f"{slug}/SKILL.md")
    if name != slug:
        raise ValueError(f"{slug}/SKILL.md name {name!r} does not match directory {slug!r}")
    _nonempty_str(fm, "description", f"{slug}/SKILL.md")


def _extract_routes_skill_refs(root: Path, routes_file: Path) -> tuple[set[str], list[str]]:
    errors: list[str] = []
    referenced: set[str] = set()
    try:
        text = _without_fenced_blocks(routes_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        return set(), [f"unreadable routes file: {exc}"]

    for span in _CODE.finditer(text):
        words = span[2].split()
        if not words:
            continue
        match = _SKILL_PATH_RX.match(words[0])
        if match:
            referenced.add(match.group(1))

    for link in _LINK.finditer(text):
        target = link.group(1).strip("<>")
        if target.startswith(("https://", "http://", "mailto:", "#")):
            continue
        target_path = target.split("#", 1)[0]
        resolved = (routes_file.parent / target_path).resolve()
        if not resolved.is_relative_to(root):
            errors.append(f"unsafe route link escaping repository: {target}")
            continue
        rel = resolved.relative_to(root).as_posix()
        match = _SKILL_PATH_RX.match(rel)
        if match:
            referenced.add(match.group(1))
    return referenced, errors


def _load_one_skill_node(root: Path, skill_dir: Path) -> SkillNode:
    slug = skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    yaml_path = skill_dir / "agents" / "openai.yaml"
    if not skill_md.is_file():
        raise ValueError(f"skill {slug!r} missing SKILL.md")
    if not yaml_path.is_file():
        raise ValueError(f"skill {slug!r} missing agents/openai.yaml")
    _parse_skill_frontmatter(skill_md, slug)

    meta = _load_yaml_dict(yaml_path)
    interface_raw = meta.get("interface")
    if not isinstance(interface_raw, dict):
        raise ValueError(f"{slug}/agents/openai.yaml missing interface mapping")
    interface = {str(k): v for k, v in cast(dict[object, object], interface_raw).items()}
    display_name = _nonempty_str(interface, "display_name", f"{slug}.interface")

    routing_raw = meta.get("routing")
    if not isinstance(routing_raw, dict):
        raise ValueError(f"{slug}/agents/openai.yaml missing routing metadata")
    routing = {str(k): v for k, v in cast(dict[object, object], routing_raw).items()}
    role = _nonempty_str(routing, "role", f"{slug}.routing")
    if role not in {"router", "specialist"}:
        raise ValueError(f"{slug}.routing role must be 'router' or 'specialist', got {role!r}")
    trigger = _nonempty_str(routing, "trigger", f"{slug}.routing")
    inputs = _string_tuple(routing, "inputs", f"{slug}.routing")
    outputs = _string_tuple(routing, "outputs", f"{slug}.routing")
    entrypoints = _string_tuple(routing, "entrypoints", f"{slug}.routing")
    for entry in entrypoints:
        if _is_repo_file_entrypoint(entry) and not _safe_path(root, entry).is_file():
            raise ValueError(f"{slug}.routing references missing entrypoint: {entry}")

    handoffs_raw = routing.get("handoffs")
    if handoffs_raw is None:
        handoffs_raw = []
    if not isinstance(handoffs_raw, list):
        raise ValueError(f"{slug}.routing.handoffs must be a list")
    handoffs: list[SkillHandoff] = []
    for idx, item_raw in enumerate(cast(list[object], handoffs_raw)):
        if not isinstance(item_raw, dict):
            raise ValueError(f"{slug}.routing.handoffs[{idx}] must be a mapping")
        item = {str(k): v for k, v in cast(dict[object, object], item_raw).items()}
        target = _nonempty_str(item, "to", f"{slug}.routing.handoffs[{idx}]")
        condition = _nonempty_str(item, "condition", f"{slug}.routing.handoffs[{idx}]")
        approval = _nonempty_str(item, "approval", f"{slug}.routing.handoffs[{idx}]")
        if target == slug:
            raise ValueError(f"{slug}.routing.handoffs[{idx}] cannot self-handoff")
        handoffs.append(
            SkillHandoff(source=slug, target=target, condition=condition, approval=approval)
        )

    return SkillNode(
        slug=slug,
        display_name=display_name,
        role=role,
        trigger=trigger,
        inputs=inputs,
        outputs=outputs,
        entrypoints=entrypoints,
        handoffs=tuple(handoffs),
    )


def load_skill_graph(root: Path) -> tuple[list[SkillNode], set[str], list[str]]:
    """Load and validate all project skills, routes, and handoff edges."""
    root = root.resolve()
    errors: list[str] = []
    skills_root = root / SKILLS_DIR
    if not skills_root.is_dir():
        return [], set(), [f"missing skills directory: {SKILLS_DIR}"]

    skill_dirs = sorted(
        p for p in skills_root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))
    )
    if not skill_dirs:
        return [], set(), [f"no skills found under {SKILLS_DIR}"]

    nodes: list[SkillNode] = []
    for skill_dir in skill_dirs:
        try:
            nodes.append(_load_one_skill_node(root, skill_dir))
        except ValueError as exc:
            errors.append(str(exc))

    routes_file = root / ROUTES_PATH
    if not routes_file.is_file():
        return nodes, set(), [*errors, f"missing route matrix file: {ROUTES_PATH}"]
    route_refs, route_errors = _extract_routes_skill_refs(root, routes_file)
    errors.extend(route_errors)

    by_slug = {node.slug: node for node in nodes}
    routers = [node for node in nodes if node.role == "router"]
    if len(routers) != 1:
        errors.append(f"expected exactly 1 router skill, found {[r.slug for r in routers]}")
        return nodes, route_refs, errors
    router = routers[0]

    for slug in sorted(route_refs):
        if slug not in by_slug:
            errors.append(f"route entry references unknown skill without a matching node: {slug}")

    for node in nodes:
        for edge in node.handoffs:
            if edge.target not in by_slug:
                errors.append(f"orphan handoff from {node.slug!r} to unknown skill {edge.target!r}")

    router_targets = {edge.target for edge in router.handoffs}
    for node in nodes:
        if node.role != "specialist":
            continue
        if node.slug not in route_refs:
            errors.append(f"specialist skill {node.slug!r} has no entry route in {ROUTES_PATH}")
        if node.slug not in router_targets:
            errors.append(
                f"router {router.slug!r} missing handoff edge to specialist {node.slug!r}"
            )
    for target in sorted(router_targets & set(by_slug)):
        if by_slug[target].role == "specialist" and target not in route_refs:
            errors.append(f"router handoff to {target!r} has no entry route in {ROUTES_PATH}")

    # BFS reachability from router ensures every skill has an active route from root.
    visited = {router.slug}
    queue: deque[str] = deque([router.slug])
    while queue:
        cur = by_slug[queue.popleft()]
        for edge in cur.handoffs:
            if edge.target in by_slug and edge.target not in visited:
                visited.add(edge.target)
                queue.append(edge.target)
    for slug in sorted(by_slug):
        if slug not in visited:
            errors.append(f"unreachable skill without route/handoff from {router.slug!r}: {slug}")

    return nodes, route_refs, errors


def _mermaid_safe(text: str, limit: int = 72) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 3].rstrip() + "..."
    return collapsed.replace('"', "'").replace("`", "")


def _render_mermaid(nodes: list[SkillNode]) -> str:
    id_map = {node.slug: f"skill_{re.sub(r'[^a-z0-9]+', '_', node.slug)}" for node in nodes}
    lines = ["flowchart TD"]
    for node in nodes:
        node_id = id_map[node.slug]
        label = (
            f"{node.slug} ({node.role})<br/>"
            f"Trigger: {_mermaid_safe(node.trigger, 58)}<br/>"
            f"Entrypoints: {len(node.entrypoints)}"
        )
        lines.append(f'  {node_id}["{label}"]')
    for node in nodes:
        src = id_map[node.slug]
        for edge in node.handoffs:
            dst = id_map[edge.target]
            edge_label = f"{_mermaid_safe(edge.condition, 46)} | Approval: {_mermaid_safe(edge.approval, 46)}"
            lines.append(f'  {src} -->|"{edge_label}"| {dst}')
    return "\n".join(lines) + "\n"


def _render_text_tree(nodes: list[SkillNode], route_refs: set[str]) -> str:
    lines: list[str] = []
    router = next(node for node in nodes if node.role == "router")
    specialists = [node for node in nodes if node.role != "router"]
    ordered = [router, *specialists]

    lines.append(f"ROUTER: {router.slug} ({router.display_name})")
    for edge in router.handoffs:
        route_tag = "routes.md:yes" if edge.target in route_refs else "routes.md:missing"
        lines.append(f"  +-> {edge.target} [{route_tag}]")
        lines.append(f"      when: {edge.condition}")
        lines.append(f"      approval: {edge.approval}")
    for spec in specialists:
        if spec.handoffs:
            lines.append(f"FEEDBACK / CROSS-SKILL HANDOFFS FROM {spec.slug}:")
            for edge in spec.handoffs:
                lines.append(f"  +-> {edge.target}")
                lines.append(f"      when: {edge.condition}")
                lines.append(f"      approval: {edge.approval}")

    lines.append("")
    lines.append("NODES:")
    for node in ordered:
        lines.append(f"- {node.slug} [{node.role}]")
        lines.append(f"  trigger: {node.trigger}")
        lines.append("  inputs:")
        for item in node.inputs:
            lines.append(f"    - {item}")
        lines.append("  outputs:")
        for item in node.outputs:
            lines.append(f"    - {item}")
        lines.append("  entrypoints:")
        for item in node.entrypoints:
            lines.append(f"    - {item}")
    return "\n".join(lines) + "\n"


def render_skill_map(root: Path, *, fmt: str = "markdown") -> str:
    """Render a deterministic skill-map diagram from tracked skill sources."""
    nodes, route_refs, errors = load_skill_graph(root)
    if errors:
        raise ValueError("cannot render invalid skill map: " + "; ".join(errors))
    mermaid = _render_mermaid(nodes).rstrip("\n")
    text_tree = _render_text_tree(nodes, route_refs).rstrip("\n")
    if fmt == "mermaid":
        return mermaid + "\n"
    if fmt == "text":
        return text_tree + "\n"
    if fmt != "markdown":
        raise ValueError(f"unsupported skill map format: {fmt!r}")
    return (
        "# Project skill map\n\n"
        "Generated from tracked `agents/openai.yaml` routing metadata and "
        "[routes](routes.md). Do not edit by hand.\n\n"
        "## Handoff diagram\n\n"
        "```mermaid\n"
        f"{mermaid}\n"
        "```\n\n"
        "## Skill waterfall and node details\n\n"
        "```text\n"
        f"{text_tree}\n"
        "```\n\n"
        "## Regeneration and validation\n\n"
        "```bash\n"
        "python3 scripts/check_investing_skill.py render-skill-map "
        f"--write {SKILL_MAP_PATH}\n"
        "python3 scripts/check_investing_skill.py check-skill-map\n"
        "```\n"
    )


def check_skill_map(root: Path, *, map_relative: str = SKILL_MAP_PATH) -> dict[str, object]:
    """Validate skill graph completeness and check skill-map.md against sources."""
    root = root.resolve()
    nodes, route_refs, errors = load_skill_graph(root)
    if not errors:
        expected = render_skill_map(root, fmt="markdown")
        try:
            map_file = _safe_path(root, map_relative)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if not map_file.is_file():
                errors.append(f"missing generated skill map: {map_relative}")
            elif map_file.read_text(encoding="utf-8") != expected:
                errors.append(f"generated skill map out of date: {map_relative}")
    return {
        "operation": "check-skill-map",
        "status": "invalid" if errors else "clean",
        "errors": errors,
        "skills": [node.slug for node in nodes],
        "edges": [
            {"from": edge.source, "to": edge.target} for node in nodes for edge in node.handoffs
        ],
        "routes": sorted(route_refs),
        "map_path": map_relative,
    }


def _record_review(
    root: Path,
    baseline_relative: str,
    payload: dict[str, object],
    report: dict[str, object],
    note: str | None,
    reviewed: list[str],
    replacements: list[str],
    additions: list[str],
) -> None:
    if not note or not note.strip():
        raise ValueError("record-review requires a semantic review note")
    entries = cast(list[dict[str, str]], payload["sources"])
    original_sources = {entry["path"]: entry["sha256"] for entry in entries}
    sources = dict(original_sources)
    renamed: set[str] = set()
    added: set[str] = set()
    for relative in additions:
        if relative in sources or relative in added:
            raise ValueError("added source must be new and named once")
        candidate = _public_source(root, relative)
        sources[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        added.add(relative)
    for replacement in replacements:
        old, separator, new = replacement.partition("=")
        if not separator or old not in sources or new in sources or old == new:
            raise ValueError("replacement must name one existing old source and a new source")
        _safe_path(root, old)
        candidate = _public_source(root, new)
        sources.pop(old)
        sources[new] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        renamed.update((old, new))
    proposed_report, _ = check(root, baseline_relative, sources)
    if proposed_report["status"] == "invalid":
        raise ValueError(
            "repair missing/unsafe sources and skill references before recording review"
        )
    changed = cast(list[dict[str, str]], proposed_report["changed_sources"])
    changed_paths = {entry["path"] for entry in changed}
    if set(reviewed) != changed_paths | renamed | added or len(reviewed) != len(set(reviewed)):
        raise ValueError("explicitly name each changed path with --reviewed-source")
    if not changed and not renamed and not added:
        return
    actual = {entry["path"]: entry["current_sha256"] for entry in changed}
    updated: dict[str, object] = {
        "schema_version": 1,
        "review_note": note.strip(),
        "sources": [
            {"path": path, "sha256": actual.get(path, digest)}
            for path, digest in sorted(sources.items())
        ],
    }
    # Refuse a concurrent source or closure change between inspection and write.
    latest, latest_payload = check(root, baseline_relative)
    latest_proposed, _ = check(root, baseline_relative, sources)
    if latest != report or latest_payload != payload or latest_proposed != proposed_report:
        raise ValueError("sources changed during review recording; run check again")
    baseline = _safe_path(root, baseline_relative)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=baseline.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(updated, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, baseline)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in {"render-skill-map", "check-skill-map"}:
        raw_argv[0] = f"--{raw_argv[0]}"

    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true", help="Read-only check (default)")
    operation.add_argument(
        "--record-review", action="store_true", help="Record completed semantic review"
    )
    operation.add_argument(
        "--render-skill-map",
        action="store_true",
        help="Render tree/waterfall skill map from metadata and routes.md",
    )
    operation.add_argument(
        "--check-skill-map",
        action="store_true",
        help="Validate skill nodes, route entries, handoffs, and skill-map.md sync",
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--format",
        choices=("markdown", "mermaid", "text"),
        default="markdown",
        help="Render-skill-map output format",
    )
    parser.add_argument(
        "--write",
        type=Path,
        default=None,
        help="Render-skill-map optional file path to write deterministically",
    )
    parser.add_argument(
        "--baseline", default=BASELINE, help="Repository-relative reviewed-source manifest"
    )
    parser.add_argument(
        "--review-note", help="Record-review only: summarize the completed semantic review"
    )
    parser.add_argument(
        "--reviewed-source",
        action="append",
        default=[],
        help="Record-review only: explicitly reviewed changed source path",
    )
    parser.add_argument(
        "--add-reviewed-source",
        action="append",
        default=[],
        help="Record-review only: new public source; also name it with --reviewed-source",
    )
    parser.add_argument(
        "--replace-reviewed-source",
        action="append",
        default=[],
        help="Record-review only: reviewed OLD=NEW source replacement; name both paths with --reviewed-source",
    )
    args = parser.parse_args(raw_argv)
    root = Path(args.repo_root).resolve()

    if args.render_skill_map:
        try:
            rendered = render_skill_map(root, fmt=str(args.format))
            if args.write is not None:
                out_path = Path(args.write)
                if not out_path.is_absolute():
                    out_path = _safe_path(root, out_path.as_posix())
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(rendered, encoding="utf-8")
            else:
                sys.stdout.write(rendered)
            return 0
        except (ValueError, OSError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 2

    if args.check_skill_map:
        skill_report = check_skill_map(root)
        print(json.dumps(skill_report, indent=2, sort_keys=True))
        return 0 if skill_report["status"] == "clean" else 2

    report, payload = check(root, str(args.baseline))
    if args.record_review and payload:
        try:
            _record_review(
                root,
                str(args.baseline),
                payload,
                report,
                args.review_note,
                args.reviewed_source,
                args.replace_reviewed_source,
                args.add_reviewed_source,
            )
            report, _ = check(root, str(args.baseline))
        except (ValueError, OSError) as exc:
            report = {**report, "status": "invalid", "errors": [str(exc)]}
    elif not args.record_review and (
        args.review_note is not None
        or args.reviewed_source
        or args.replace_reviewed_source
        or args.add_reviewed_source
    ):
        report = {
            **report,
            "status": "invalid",
            "errors": ["review arguments require record-review"],
        }
    print(
        json.dumps(
            {"operation": "record-review" if args.record_review else "check", **report},
            indent=2,
            sort_keys=True,
        )
    )
    return {"clean": 0, "drift": 1, "invalid": 2}[str(report["status"])]


if __name__ == "__main__":
    raise SystemExit(main())

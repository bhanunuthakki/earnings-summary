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
import tempfile
from pathlib import Path, PurePosixPath
from typing import cast

SKILL = "src/advisor/skills/earnings-summary-investing"
BASELINE = f"{SKILL}/references/reviewed-sources.json"
_LINK = re.compile(r"\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)")
_CODE = re.compile(r"`([^`]+)`")
_ROOT_FILES = {"AGENTS.md", "DEFINITIONS.md", "reconstruction_manifest.json"}
_SOURCE_DIRS = {"directives", "execution", "src", "scripts"}


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
            source = _safe_path(root, relative)
            if not source.is_file():
                raise ValueError("source is missing or not a file")
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
            text = file.read_text(encoding="utf-8")
            for raw in _CODE.findall(text):
                relative = raw.split()[0]
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
    status = "invalid" if errors else "drift" if changed else "clean"
    return {"status": status, "errors": errors, "changed_sources": changed}, payload


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
    for replacement in replacements:
        old, separator, new = replacement.partition("=")
        if not separator or old not in sources or new in sources or old == new:
            raise ValueError("replacement must name one existing old source and a new source")
        _safe_path(root, old)
        candidate = _safe_path(root, new)
        if not candidate.is_file():
            raise ValueError("replacement source must be a safe existing file")
        if new not in _ROOT_FILES and PurePosixPath(new).parts[0] not in _SOURCE_DIRS:
            raise ValueError("replacement is outside public procedure/code scope")
        if candidate.suffix not in {".py", ".md", ".json", ".js"}:
            raise ValueError("unsupported replacement source type")
        sources.pop(old)
        sources[new] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        renamed.update((old, new))
    for new in additions:
        if new in original_sources or new in sources:
            raise ValueError("addition must name one new, unlisted source without duplicates")
        candidate = _safe_path(root, new)
        if not candidate.is_file():
            raise ValueError("addition source must be a safe existing file")
        if new not in _ROOT_FILES and PurePosixPath(new).parts[0] not in _SOURCE_DIRS:
            raise ValueError("addition is outside public procedure/code scope")
        if candidate.suffix not in {".py", ".md", ".json", ".js"}:
            raise ValueError("unsupported addition source type")
        sources[new] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        added.add(new)
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
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true", help="Read-only check (default)")
    operation.add_argument(
        "--record-review", action="store_true", help="Record completed semantic review"
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
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
        "--replace-reviewed-source",
        action="append",
        default=[],
        help="Record-review only: reviewed OLD=NEW source replacement; name both paths with --reviewed-source",
    )
    parser.add_argument(
        "--add-reviewed-source",
        action="append",
        default=[],
        help="Record-review only: new reviewed public source; also name it with --reviewed-source",
    )
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
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

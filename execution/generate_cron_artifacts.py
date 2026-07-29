"""Generate and validate Task Scheduler artifacts from the canonical manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scheduler_manifest import (  # noqa: E402
    bootstrap_manifest,
    dump_manifest,
    generated_inventory_markdown,
    generated_registration_script,
    load_manifest,
    rendered_xml_bytes,
    validate_source_tree,
)

DEFAULT_MANIFEST = PROJECT_ROOT / "cron" / "task_manifest.json"
DEFAULT_SCRIPT = PROJECT_ROOT / "cron" / "register_tasks.generated.ps1"
DEFAULT_DOC = PROJECT_ROOT / "cron" / "TASKS.generated.md"


def _check_text(path: Path, expected: str) -> str | None:
    if not path.is_file():
        return f"{path}: missing generated artifact"
    actual = path.read_text(encoding="utf-8")
    if actual != expected:
        return f"{path}: generated artifact is stale"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate source coverage and fail if generated tracked artifacts are stale.",
    )
    parser.add_argument(
        "--bootstrap-manifest",
        action="store_true",
        help="One-time mechanical import of the current XML fleet into --manifest.",
    )
    args = parser.parse_args(argv)

    project_root = args.project_root.resolve()
    cron_dir = project_root / "cron"
    manifest_path = args.manifest.resolve()
    if args.bootstrap_manifest:
        manifest_path.write_text(
            dump_manifest(bootstrap_manifest(cron_dir)),
            encoding="utf-8",
            newline="\n",
        )

    manifest = load_manifest(manifest_path)
    errors = validate_source_tree(manifest, cron_dir=cron_dir)
    registration_script = generated_registration_script(manifest)
    inventory_doc = generated_inventory_markdown(manifest)

    if args.check:
        for path, expected in (
            (project_root / "cron" / DEFAULT_SCRIPT.name, registration_script),
            (project_root / "cron" / DEFAULT_DOC.name, inventory_doc),
        ):
            error = _check_text(path, expected)
            if error is not None:
                errors.append(error)
    else:
        (project_root / "cron" / DEFAULT_SCRIPT.name).write_text(
            registration_script, encoding="utf-8", newline="\n"
        )
        (project_root / "cron" / DEFAULT_DOC.name).write_text(
            inventory_doc, encoding="utf-8", newline="\n"
        )

    if args.render_dir is not None:
        render_dir = args.render_dir.resolve()
        render_dir.mkdir(parents=True, exist_ok=True)
        for task in manifest.tasks:
            (render_dir / task.xml).write_bytes(
                rendered_xml_bytes(task, cron_dir=cron_dir, project_root=project_root)
            )

    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"validated {len(manifest.tasks)} scheduled tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

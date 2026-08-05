"""Seal one complete filing-XBRL runtime and AppContainer launcher manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.inline_xbrl_processor import (  # noqa: E402
    ProcessorBundleManifest,
    RuntimeArtifactMember,
    enumerate_closed_local_tree,
    runtime_artifact_set_sha256,
)
from log_redact import redact  # noqa: E402
from provenance.immutable_artifact import (  # noqa: E402
    canonical_text_artifact_sha256,
    path_aliases_any,
    publish_text_no_clobber,
    read_stable_artifact,
    require_no_reparse_points,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FilingXbrlBundleBuildRequest(_FrozenModel):
    template: Path
    runtime_root: Path
    sandbox_launcher: Path
    output: Path


class FilingXbrlBundleBuildResult(_FrozenModel):
    schema_version: str = "filing-xbrl-bundle-build-result/v1"
    output_path: str
    output_sha256: str
    manifest_sha256: str
    runtime_artifact_sha256: str
    runtime_member_count: int
    runtime_byte_count: int
    sandbox_launcher_sha256: str
    published: bool


def build_filing_xbrl_processor_bundle(
    request: FilingXbrlBundleBuildRequest,
) -> FilingXbrlBundleBuildResult:
    """Hash every runtime byte once and immutably publish the exact manifest."""

    template = Path(os.path.abspath(request.template))
    runtime_root = Path(os.path.abspath(request.runtime_root))
    launcher = Path(os.path.abspath(request.sandbox_launcher))
    output = Path(os.path.abspath(request.output))
    for path in (template, runtime_root, launcher, output):
        require_no_reparse_points(path)
    if path_aliases_any(output, {template, launcher}):
        raise ValueError("bundle manifest output aliases an admitted input")
    if not runtime_root.is_dir():
        raise ValueError("bundle runtime root is unavailable")
    root_before = _directory_identity(runtime_root)
    _template_snapshot, template_bytes = read_stable_artifact(template)
    launcher_snapshot, _launcher_bytes = read_stable_artifact(launcher)
    if launcher.stat().st_nlink != 1:
        raise ValueError("bundle sandbox launcher has a hardlink alias")
    manifest = ProcessorBundleManifest.model_validate_json(template_bytes)
    bridge_source = PROJECT_ROOT / "execution" / "filing_xbrl_bridge.py"
    launcher_source = PROJECT_ROOT / "execution" / "filing_xbrl_appcontainer_launcher.cs"
    for source in (bridge_source, launcher_source):
        require_no_reparse_points(source)
        if source.stat().st_nlink != 1:
            raise ValueError("bundle reviewed source has a hardlink alias")
    bridge_source_snapshot, bridge_source_bytes = read_stable_artifact(bridge_source)
    launcher_source_snapshot, _launcher_source_bytes = read_stable_artifact(launcher_source)

    candidates = enumerate_closed_local_tree(
        runtime_root,
        label="bundle runtime",
    )
    members: list[RuntimeArtifactMember] = []
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(runtime_root).as_posix()
        if path.suffix.casefold() == ".pyc" or "__pycache__" in path.parts:
            raise ValueError("bundle runtime contains mutable Python bytecode")
        if path.stat().st_nlink != 1:
            raise ValueError("bundle runtime contains a hardlink alias")
        snapshot, _payload = read_stable_artifact(path)
        members.append(
            RuntimeArtifactMember(
                relative_path=relative,
                blob_sha256=snapshot.file_sha256,
                byte_size=snapshot.size_bytes,
            )
        )
    members_tuple = tuple(members)
    if not members_tuple:
        raise ValueError("bundle runtime is empty")
    if not any(member.relative_path.startswith("offline-cache/") for member in members_tuple):
        raise ValueError("bundle runtime has no sealed offline taxonomy cache")
    if tuple(member.relative_path for member in members_tuple) != tuple(
        sorted(member.relative_path for member in members_tuple)
    ):
        raise ValueError("bundle runtime enumeration is not canonical")
    observed_after = tuple(
        path.relative_to(runtime_root).as_posix()
        for path in enumerate_closed_local_tree(
            runtime_root,
            label="bundle runtime",
        )
        if path.is_file()
    )
    if observed_after != tuple(member.relative_path for member in members_tuple):
        raise ValueError("bundle runtime membership changed during hashing")
    if root_before != _directory_identity(runtime_root):
        raise ValueError("bundle runtime root changed during hashing")
    python_members = tuple(
        member
        for member in members_tuple
        if member.relative_path == manifest.execution.bundle_python_relative_path
    )
    if len(python_members) != 1:
        raise ValueError("bundle Python is absent from the runtime closure")
    bridge_relative = manifest.bridge_module.replace(".", "/") + ".py"
    bridge_members = tuple(
        member for member in members_tuple if member.relative_path == bridge_relative
    )
    if len(bridge_members) != 1:
        raise ValueError("bundle bridge module is absent from the runtime closure")
    runtime_bridge = runtime_root / Path(bridge_relative)
    _runtime_bridge_snapshot, runtime_bridge_bytes = read_stable_artifact(runtime_bridge)
    if runtime_bridge_bytes != bridge_source_bytes:
        raise ValueError("bundle bridge bytes differ from the reviewed source")
    runtime_sha = runtime_artifact_set_sha256(members_tuple)
    execution = manifest.execution.model_copy(
        update={
            "bundle_python_sha256": python_members[0].blob_sha256,
            "runtime_members": members_tuple,
            "runtime_artifact_sha256": runtime_sha,
            "sandbox_launcher_sha256": launcher_snapshot.file_sha256,
        }
    )
    build_provenance = manifest.build_provenance.model_copy(
        update={
            "bridge_source_sha256": bridge_source_snapshot.file_sha256,
            "launcher_source_sha256": launcher_source_snapshot.file_sha256,
        }
    )
    sealed = manifest.model_copy(
        update={"execution": execution, "build_provenance": build_provenance}
    )
    canonical = sealed.canonical_json
    published = publish_text_no_clobber(output, canonical)
    return FilingXbrlBundleBuildResult(
        output_path=str(output),
        output_sha256=canonical_text_artifact_sha256(canonical),
        manifest_sha256=sealed.manifest_sha256,
        runtime_artifact_sha256=runtime_sha,
        runtime_member_count=len(members_tuple),
        runtime_byte_count=sum(member.byte_size for member in members_tuple),
        sandbox_launcher_sha256=launcher_snapshot.file_sha256,
        published=published,
    )


def _directory_identity(path: Path) -> tuple[int, int, int, int, int]:
    observed = path.stat()
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        int(observed.st_size),
        int(observed.st_mtime_ns),
        int(observed.st_ctime_ns),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--sandbox-launcher", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_filing_xbrl_processor_bundle(
            FilingXbrlBundleBuildRequest(
                template=args.template,
                runtime_root=args.runtime_root,
                sandbox_launcher=args.sandbox_launcher,
                output=args.output,
            )
        )
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "error": redact(str(exc)),
                    "error_type": type(exc).__name__,
                    "event": "filing_xbrl_bundle_build_refused",
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

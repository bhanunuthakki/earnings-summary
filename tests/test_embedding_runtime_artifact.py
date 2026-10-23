from __future__ import annotations

import os
from pathlib import Path

import pytest

from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactSource,
    build_runtime_artifact,
    parse_runtime_artifact,
    verify_runtime_artifact,
)
from search.local_vector import (
    EmbeddingModelSpec,
    FastEmbedEncoder,
    LocalVectorCapabilityError,
)


def _build(root: Path, *, reverse: bool = False) -> EmbeddingRuntimeArtifact:
    sources = [
        RuntimeArtifactSource(
            logical_name="model/model.onnx",
            role="model",
            relative_path=Path("physical-model.bin"),
        ),
        RuntimeArtifactSource(
            logical_name="tokenizer/tokenizer.json",
            role="tokenizer",
            relative_path=Path("physical-tokenizer.bin"),
        ),
    ]
    return build_runtime_artifact(
        root,
        list(reversed(sources)) if reverse else sources,
        provider="fastembed",
        model="acme/local-model",
        dimensions=2,
        execution_provider="CPUExecutionProvider",
        execution_settings={"threads": 1},
        component_versions={"fastembed": "1.2.3", "onnxruntime": "4.5.6"},
    )


def _files(root: Path) -> None:
    root.mkdir()
    (root / "physical-model.bin").write_bytes(b"model")
    (root / "physical-tokenizer.bin").write_bytes(b"tokenizer")


def test_manifest_is_path_and_input_order_invariant(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "different-physical-root"
    _files(left)
    _files(right)

    first = _build(left)
    second = _build(right, reverse=True)

    assert first == second
    assert first.sha256() == second.sha256()
    canonical = first.canonical_json()
    assert str(left) not in canonical
    assert str(right) not in canonical
    assert parse_runtime_artifact(canonical, first.sha256()) == first


def test_byte_drift_fails_exact_reverification(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _files(root)
    artifact = _build(root)
    (root / "physical-model.bin").write_bytes(b"changed")

    with pytest.raises(ValueError, match="no longer matches"):
        verify_runtime_artifact(
            artifact,
            root,
            (
                RuntimeArtifactSource(
                    logical_name="model/model.onnx",
                    role="model",
                    relative_path=Path("physical-model.bin"),
                ),
                RuntimeArtifactSource(
                    logical_name="tokenizer/tokenizer.json",
                    role="tokenizer",
                    relative_path=Path("physical-tokenizer.bin"),
                ),
            ),
        )


def test_duplicate_alias_missing_and_nonregular_sources_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    _files(root)
    duplicate = RuntimeArtifactSource(
        logical_name="other/model.onnx",
        role="model",
        relative_path=Path("physical-model.bin"),
    )
    with pytest.raises(ValueError, match="aliases"):
        build_runtime_artifact(
            root,
            (
                RuntimeArtifactSource(
                    logical_name="model/model.onnx",
                    role="model",
                    relative_path=Path("physical-model.bin"),
                ),
                duplicate,
            ),
            provider="fastembed",
            model="model",
            dimensions=2,
            execution_provider="CPUExecutionProvider",
            execution_settings={},
            component_versions={"fastembed": "1"},
        )
    (root / "directory").mkdir()
    for bad_path in (Path("missing"), Path("directory")):
        with pytest.raises(ValueError):
            build_runtime_artifact(
                root,
                (
                    RuntimeArtifactSource(
                        logical_name="model/model.onnx",
                        role="model",
                        relative_path=bad_path,
                    ),
                ),
                provider="fastembed",
                model="model",
                dimensions=2,
                execution_provider="CPUExecutionProvider",
                execution_settings={},
                component_versions={"fastembed": "1"},
            )


def test_symlink_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = root / "model.bin"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="escapes"):
        build_runtime_artifact(
            root,
            (
                RuntimeArtifactSource(
                    logical_name="model/model.onnx",
                    role="model",
                    relative_path=Path("model.bin"),
                ),
            ),
            provider="fastembed",
            model="model",
            dimensions=2,
            execution_provider="CPUExecutionProvider",
            execution_settings={},
            component_versions={"fastembed": "1"},
        )


def test_fastembed_adapter_is_explicit_offline_and_verifies_after_load(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    _files(root)
    artifact = _build(root)
    calls: list[dict[str, object]] = []

    class FakeTextEmbedding:
        def __init__(
            self,
            *,
            model_name: str,
            cache_dir: Path,
            local_files_only: bool,
            providers: list[str],
            threads: int,
        ) -> None:
            calls.append(
                {
                    "model_name": model_name,
                    "cache_dir": cache_dir,
                    "local_files_only": local_files_only,
                    "providers": providers,
                    "threads": threads,
                }
            )

    class FakeModule:
        TextEmbedding = FakeTextEmbedding

    runtime_sources = (
        RuntimeArtifactSource(
            logical_name="model/model.onnx",
            role="model",
            relative_path=Path("physical-model.bin"),
        ),
        RuntimeArtifactSource(
            logical_name="tokenizer/tokenizer.json",
            role="tokenizer",
            relative_path=Path("physical-tokenizer.bin"),
        ),
    )
    FastEmbedEncoder.from_spec(
        EmbeddingModelSpec(provider="fastembed", model="acme/local-model", dimensions=2),
        runtime_artifact=artifact,
        runtime_root=root,
        sources=runtime_sources,
        importer=lambda name: FakeModule(),
        version_lookup=lambda name: {"fastembed": "1.2.3", "onnxruntime": "4.5.6"}[name],
    )
    assert calls == [
        {
            "model_name": "acme/local-model",
            "cache_dir": root,
            "local_files_only": True,
            "providers": ["CPUExecutionProvider"],
            "threads": 1,
        }
    ]

    class MutatingTextEmbedding(FakeTextEmbedding):
        def __init__(
            self,
            *,
            model_name: str,
            cache_dir: Path,
            local_files_only: bool,
            providers: list[str],
            threads: int,
        ) -> None:
            super().__init__(
                model_name=model_name,
                cache_dir=cache_dir,
                local_files_only=local_files_only,
                providers=providers,
                threads=threads,
            )
            (root / "physical-model.bin").write_bytes(b"mutated")

    class MutatingModule:
        TextEmbedding = MutatingTextEmbedding

    with pytest.raises(LocalVectorCapabilityError, match="failed closed"):
        FastEmbedEncoder.from_spec(
            EmbeddingModelSpec(provider="fastembed", model="acme/local-model", dimensions=2),
            runtime_artifact=artifact,
            runtime_root=root,
            sources=runtime_sources,
            importer=lambda name: MutatingModule(),
            version_lookup=lambda name: {
                "fastembed": "1.2.3",
                "onnxruntime": "4.5.6",
            }[name],
        )


def test_fastembed_adapter_accepts_kwargs_only_local_files_only(tmp_path: Path) -> None:
    """fastembed 0.9.0 ships local_files_only through **kwargs, not a named parameter.

    The guard accepts that shape while keeping offline loading verifiable: the
    constructed instance must report the resolved local-only mode, so the flag
    cannot be silently swallowed by an unrelated **kwargs signature.
    """
    root = tmp_path / "runtime"
    _files(root)
    artifact = _build(root)
    calls: list[dict[str, object]] = []

    class KwargsTextEmbedding:
        # Mirrors fastembed 0.9.0: local_files_only is not a named constructor
        # parameter; the base class pops it from **kwargs and records the
        # resolved mode as _local_files_only on the instance.
        def __init__(
            self,
            model_name: str,
            cache_dir: Path,
            threads: int | None = None,
            providers: list[str] | None = None,
            cuda: bool = False,
            device_ids: list[int] | None = None,
            lazy_load: bool = False,
            **kwargs: object,
        ) -> None:
            self._local_files_only = bool(kwargs.pop("local_files_only", False))
            calls.append({"model_name": model_name, "local_files_only": self._local_files_only})

    class KwargsModule:
        TextEmbedding = KwargsTextEmbedding

    encoder = FastEmbedEncoder.from_spec(
        EmbeddingModelSpec(provider="fastembed", model="acme/local-model", dimensions=2),
        runtime_artifact=artifact,
        runtime_root=root,
        sources=(
            RuntimeArtifactSource(
                logical_name="model/model.onnx",
                role="model",
                relative_path=Path("physical-model.bin"),
            ),
            RuntimeArtifactSource(
                logical_name="tokenizer/tokenizer.json",
                role="tokenizer",
                relative_path=Path("physical-tokenizer.bin"),
            ),
        ),
        importer=lambda name: KwargsModule(),
        version_lookup=lambda name: {"fastembed": "1.2.3", "onnxruntime": "4.5.6"}[name],
    )
    assert isinstance(encoder, FastEmbedEncoder)
    assert calls == [{"model_name": "acme/local-model", "local_files_only": True}]


def test_fastembed_adapter_fails_closed_when_kwargs_swallow_local_files_only(
    tmp_path: Path,
) -> None:
    """A **kwargs signature alone is not proof: an instance that never reports
    local-only mode must fail closed instead of looking like an offline load."""
    root = tmp_path / "runtime"
    _files(root)
    artifact = _build(root)

    class SwallowingTextEmbedding:
        def __init__(
            self,
            model_name: str,
            cache_dir: Path,
            threads: int | None = None,
            providers: list[str] | None = None,
            **kwargs: object,
        ) -> None:
            self.model_name = model_name

    class SwallowingModule:
        TextEmbedding = SwallowingTextEmbedding

    with pytest.raises(LocalVectorCapabilityError, match="local-only loading"):
        FastEmbedEncoder.from_spec(
            EmbeddingModelSpec(provider="fastembed", model="acme/local-model", dimensions=2),
            runtime_artifact=artifact,
            runtime_root=root,
            sources=(
                RuntimeArtifactSource(
                    logical_name="model/model.onnx",
                    role="model",
                    relative_path=Path("physical-model.bin"),
                ),
                RuntimeArtifactSource(
                    logical_name="tokenizer/tokenizer.json",
                    role="tokenizer",
                    relative_path=Path("physical-tokenizer.bin"),
                ),
            ),
            importer=lambda name: SwallowingModule(),
            version_lookup=lambda name: {"fastembed": "1.2.3", "onnxruntime": "4.5.6"}[name],
        )


def test_fastembed_adapter_fails_closed_without_any_offline_parameter_path(
    tmp_path: Path,
) -> None:
    """Neither a named local_files_only parameter nor **kwargs means offline
    loading cannot be requested at all; the adapter must fail closed."""
    root = tmp_path / "runtime"
    _files(root)
    artifact = _build(root)

    class LegacyTextEmbedding:
        def __init__(
            self,
            model_name: str,
            cache_dir: Path,
            threads: int | None = None,
            providers: list[str] | None = None,
        ) -> None:
            self.model_name = model_name

    class LegacyModule:
        TextEmbedding = LegacyTextEmbedding

    with pytest.raises(LocalVectorCapabilityError, match="local-only loading"):
        FastEmbedEncoder.from_spec(
            EmbeddingModelSpec(provider="fastembed", model="acme/local-model", dimensions=2),
            runtime_artifact=artifact,
            runtime_root=root,
            sources=(
                RuntimeArtifactSource(
                    logical_name="model/model.onnx",
                    role="model",
                    relative_path=Path("physical-model.bin"),
                ),
                RuntimeArtifactSource(
                    logical_name="tokenizer/tokenizer.json",
                    role="tokenizer",
                    relative_path=Path("physical-tokenizer.bin"),
                ),
            ),
            importer=lambda name: LegacyModule(),
            version_lookup=lambda name: {"fastembed": "1.2.3", "onnxruntime": "4.5.6"}[name],
        )

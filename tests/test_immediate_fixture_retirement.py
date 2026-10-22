"""Small isolated tests for exact-run producer closure, without pytest bootstrap."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, cast
from unittest.mock import patch

STAGED = Path(__file__).resolve().parent
if STAGED.name == "tests":
    from src.operations import artifact_retention as artifacts
    from src.operations import temp_run_retention as runs
else:
    for name in ("artifact_retention", "storage_reservation", "temp_run_retention"):
        full_name = "src.operations." + name
        spec = importlib.util.spec_from_file_location(full_name, STAGED / (name + ".py"))
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)

    artifacts = sys.modules["src.operations.artifact_retention"]
    runs = sys.modules["src.operations.temp_run_retention"]


class ImmediateFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="earnings-summary-immediate-unit-")
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name).resolve() / "repo"
        self.repo.mkdir()
        self.root = self.repo / ".tmp" / "owned-run"
        self.env = patch.dict(
            os.environ, {"EARNINGS_SUMMARY_DB_PATH": str(self.repo / "data/live.db")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        runs.begin_temp_run(
            self.root,
            repo_root=self.repo,
            disposable_class="isolated_test_fixture",
            purpose="bounded immediate retirement regression",
            min_free_bytes=0,
        )

    def payload(self, name: str = "fixtures/payload.bin") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"small owned synthetic payload")
        return path

    def seal(self, success: bool = True):
        return runs.finish_temp_run(
            self.root,
            repo_root=self.repo,
            success=success,
            disposable_paths=self.root.rglob("*"),
            allow_partial=True,
        )

    def retire(self):
        return runs.retire_completed_temp_run(self.root, repo_root=self.repo, apply=True)

    def test_success_retires_exact_payload_and_keeps_manifest_journal(self):
        payload = self.payload()
        manifest = self.seal()
        manifest_bytes = (self.root / runs.MANIFEST_NAME).read_bytes()
        result = runs.retire_completed_temp_run(
            self.root,
            repo_root=self.repo,
            apply=True,
            now=manifest.completed_at,
        )
        self.assertFalse(payload.exists())
        self.assertEqual(result.deleted, 1)
        self.assertEqual((self.root / runs.MANIFEST_NAME).read_bytes(), manifest_bytes)
        assert result.receipt_path is not None
        receipt = Path(result.receipt_path)
        self.assertIn(self.root, receipt.parents)
        self.assertEqual(json.loads(receipt.read_text())["state"], "completed")
        self.assertTrue(receipt.with_suffix(".jsonl").exists())
        self.assertFalse((self.repo / "data/operations/artifact-retention-receipts").exists())

    def test_failed_run_cannot_retire_and_retains_original_manifest(self):
        payload = self.payload()
        self.seal(success=False)
        before = (self.root / runs.MANIFEST_NAME).read_bytes()
        with self.assertRaisesRegex(ValueError, "completed isolated fixture"):
            self.retire()
        self.assertTrue(payload.exists())
        self.assertEqual((self.root / runs.MANIFEST_NAME).read_bytes(), before)

    def test_active_and_unknown_runs_cannot_retire(self):
        payload = self.payload()
        with self.assertRaisesRegex(ValueError, "completed isolated fixture"):
            self.retire()
        self.assertTrue(payload.exists())
        unknown = self.repo / ".tmp/unknown-run"
        unknown.mkdir()
        evidence = unknown / "payload.bin"
        evidence.write_bytes(b"unknown owner")
        with self.assertRaises(FileNotFoundError):
            runs.retire_completed_temp_run(unknown, repo_root=self.repo, apply=True)
        self.assertTrue(evidence.exists())

    def test_unrelated_operator_candidate_and_same_run_unknown_remain(self):
        payload = self.payload()
        self.seal()
        unknown = self.payload("new-after-closure.bin")
        other = self.repo / ".tmp/operator/payload.bin"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"operator evidence")
        item = artifacts.Artifact(
            path=other,
            allowed_root=other.parent,
            family="operator",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            sha256=hashlib.sha256(other.read_bytes()).hexdigest(),
            size=other.stat().st_size,
            kind="disposable_test",
            status="completed",
            verified=True,
        )
        catalog = self.repo / artifacts.CATALOG_RELATIVE_PATH
        catalog.parent.mkdir(parents=True)
        catalog.write_text(
            artifacts.ArtifactCatalog(schema_version=1, artifacts=[item]).model_dump_json()
        )
        result = self.retire()
        self.assertFalse(payload.exists())
        self.assertTrue(other.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual([decision.path for decision in result.decisions], [str(payload)])

    def test_overlapping_operator_hold_retains_exact_fixture(self):
        payload = self.payload()
        self.seal()
        item = artifacts.Artifact(
            path=payload,
            allowed_root=self.root,
            family="operator",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
            size=payload.stat().st_size,
            kind="disposable_test",
            status="completed",
            verified=True,
            pins=["owner_review"],
        )
        catalog = self.repo / artifacts.CATALOG_RELATIVE_PATH
        catalog.parent.mkdir(parents=True)
        catalog.write_text(
            artifacts.ArtifactCatalog(schema_version=1, artifacts=[item]).model_dump_json()
        )
        result = self.retire()
        self.assertTrue(payload.exists())
        self.assertEqual(result.decisions[0].action, "keep")

    def test_changed_links_external_git_archive_and_live_files_held(self):
        safe = self.payload("safe.bin")
        changed = self.payload("changed.bin")
        archive = self.payload("portfolio_gc_archive.db")
        live = self.payload("live.db")
        external = self.repo / "outside"
        external.mkdir()
        outside = external / "sentinel"
        outside.write_bytes(b"outside owned run")
        alias = self.root / "alias"
        alias.symlink_to(outside)
        git_fixture = self.root / "external-git-fixture"
        git_fixture.mkdir()
        (git_fixture / ".git").write_text("gitdir: " + str(external))
        git_payload = self.payload("external-git-fixture/source.py")
        with patch.dict(os.environ, {"EARNINGS_SUMMARY_DB_PATH": str(live)}):
            manifest = self.seal()
            changed.write_bytes(b"changed after verified closure")
            result = self.retire()
        self.assertFalse(safe.exists())
        for retained in [changed, archive, live, alias, outside, git_payload]:
            self.assertTrue(retained.exists())
        self.assertTrue(manifest.held_paths)
        self.assertEqual(next(d for d in result.decisions if d.path == str(changed)).action, "keep")

    def test_changed_before_unlink_is_journaled_as_error(self):
        payload = self.payload()
        self.seal()
        original = vars(artifacts)["_write_receipt"]

        def change_after_plan(path: Path, value: dict[str, object]) -> None:
            original(path, value)
            payload.write_bytes(b"changed while preparing retirement journal")

        with patch.object(artifacts, "_write_receipt", side_effect=change_after_plan):
            result = self.retire()
        self.assertTrue(payload.exists())
        self.assertEqual(result.errors, 1)
        assert result.receipt_path is not None
        self.assertEqual(json.loads(Path(result.receipt_path).read_text())["state"], "failed")

    def test_manifest_parse_and_index_calls_are_bounded_for_200_files(self):
        for number in range(200):
            self.payload(f"fixtures/payload-{number}.bin")
        self.seal()
        manifest = self.root / runs.MANIFEST_NAME
        manifest_bytes = manifest.read_bytes()
        original_open = Path.open
        original_sha256 = hashlib.sha256
        manifest_reads = initial_hashes = 0

        def counted_open(
            path: Path,
            mode: str = "r",
            buffering: int = -1,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> IO[Any]:
            nonlocal manifest_reads
            if path == manifest and mode == "rb":
                manifest_reads += 1
            return original_open(path, mode, buffering, encoding, errors, newline)

        def counted_sha256(data: bytes = b"", *, usedforsecurity: bool = True) -> object:
            nonlocal initial_hashes
            if data == manifest_bytes:
                initial_hashes += 1
            return original_sha256(data, usedforsecurity=usedforsecurity)

        vars(artifacts)["_MANIFEST_CACHE"].clear()
        with (
            patch.object(Path, "open", counted_open),
            patch.object(hashlib, "sha256", side_effect=counted_sha256),
            patch.object(artifacts, "_hash", wraps=vars(artifacts)["_hash"]) as file_hash,
            patch.object(
                artifacts, "_decode_manifest", wraps=vars(artifacts)["_decode_manifest"]
            ) as decode,
            patch.object(
                artifacts,
                "_fixture_file_matches",
                wraps=vars(artifacts)["_fixture_file_matches"],
            ) as matches,
        ):
            result = self.retire()
        self.assertEqual(result.deleted, 200)
        self.assertEqual(decode.call_count, 1)
        self.assertLessEqual(matches.call_count, 600)
        self.assertLessEqual(len(vars(artifacts)["_MANIFEST_CACHE"]), 2)
        # Two fresh reads/hashes per validation, planning plus apply, and three
        # caller reads/hashes bind the exact manifest before retirement starts.
        full_hashes = initial_hashes + sum(
            call.args[0] == manifest for call in file_hash.call_args_list
        )
        self.assertLessEqual(manifest_reads, 803, f"reads={manifest_reads}, hashes={full_hashes}")
        self.assertLessEqual(full_hashes, 803)

    def test_manifest_pin_added_during_apply_hash_with_preserved_metadata_is_held(self):
        payload = self.payload()
        self.seal()
        path = self.root / runs.MANIFEST_NAME
        original = path.read_bytes() + b" " * 128
        path.write_bytes(original)
        metadata = path.stat()
        mutated = json.loads(original)
        mutated["pins"] = ["new owner hold"]
        changed = json.dumps(mutated, separators=(",", ":")).encode()
        self.assertLessEqual(len(changed), len(original))
        changed += b" " * (len(original) - len(changed))
        original_hash = vars(artifacts)["_hash"]
        payload_hashes = 0

        def mutate_during_apply_hash(current: Path) -> str:
            nonlocal payload_hashes
            digest = original_hash(current)
            if current == payload:
                payload_hashes += 1
                if payload_hashes == 2:
                    path.write_bytes(changed)
                    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            return digest

        with patch.object(artifacts, "_hash", side_effect=mutate_during_apply_hash):
            result = self.retire()
        self.assertEqual(payload_hashes, 2)
        self.assertEqual(path.stat().st_size, metadata.st_size)
        self.assertEqual(path.stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertTrue(payload.exists())
        self.assertEqual(result.deleted, 0)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.decisions[0].reason, "lifecycle_manifest_changed")

    def test_changed_manifest_hash_is_held_after_cache_population(self):
        payload = self.payload()
        self.seal()
        original = vars(artifacts)["_write_receipt"]

        def change_manifest(path: Path, value: dict[str, object]) -> None:
            original(path, value)
            manifest_path = self.root / runs.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_bytes())
            manifest["pins"] = ["new_owner_hold"]
            manifest_path.write_text(json.dumps(manifest))

        with patch.object(artifacts, "_write_receipt", side_effect=change_manifest):
            result = self.retire()
        self.assertTrue(payload.exists())
        self.assertEqual(result.deleted, 0)
        self.assertEqual(result.errors, 1)

    def test_duplicate_manifest_declarations_are_held(self):
        payload = self.payload()
        self.seal()
        manifest_path = self.root / runs.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"].append(manifest["files"][0])
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "duplicate disposable paths"):
            self.retire()
        item = artifacts.Artifact(
            path=payload,
            allowed_root=self.root,
            family="test_tmp",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
            size=payload.stat().st_size,
            kind="disposable_test",
            status="completed",
            verified=True,
            disposable_class="isolated_test_fixture",
            lifecycle_manifest=manifest_path,
            lifecycle_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
        result = artifacts.run_retention(
            self.repo,
            now=datetime.now(UTC),
            apply=True,
            catalog=artifacts.ArtifactCatalog(schema_version=1, artifacts=[item]),
            exact_run_root=self.root,
            test_retention_days=0,
        )
        self.assertTrue(payload.exists())
        self.assertEqual(result.deleted, 0)
        self.assertTrue(result.decisions)
        self.assertTrue(all(decision.action == "keep" for decision in result.decisions))

    def test_actual_sessionfinish_success_and_failure_paths(self):
        tree = ast.parse((STAGED / "conftest.py").read_text())
        functions = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and n.name
            in {
                "pytest_sessionfinish",
                "_collection_disposable_paths",
                "_retire_collection_temp_run",
            }
        ]
        for function in functions:
            function.decorator_list = []
        callbacks: list[Callable[[], None]] = []
        namespace: dict[str, object] = {
            "pytest": SimpleNamespace(Session=object),
            "_test_db_dir": self.root,
            "_test_code_root": self.repo,
            "_managed_test_temps": True,
            "_test_db_path": self.root / "collection.db",
            "Iterator": Iterator,
            "Path": Path,
            "os": os,
            "stat": stat,
            "atexit": SimpleNamespace(register=callbacks.append),
        }
        exec(
            compile(
                ast.Module(body=list(functions), type_ignores=[]),
                "staged_sessionfinish",
                "exec",
            ),
            namespace,
        )
        payload = self.payload()
        fixture_database = self.payload("fixtures/portfolio.db")
        junit = self.payload("fixtures/junit.xml")
        terminal = self.payload("fixtures/terminal.log")
        unknown = self.payload("session-output.json")
        sessionfinish = cast(Callable[[object, int], None], namespace["pytest_sessionfinish"])
        # Off a GitHub Actions runner, success registers the exit retirement.
        with patch.dict(os.environ):
            os.environ.pop("GITHUB_ACTIONS", None)
            sessionfinish(None, 0)
        self.assertTrue(
            payload.exists(),
            "session/plugin consumers retain payload until process exit",
        )
        self.assertEqual(len(callbacks), 1)
        with patch.dict(os.environ):
            os.environ.pop("EARNINGS_SUMMARY_DB_PATH", None)
            callbacks.pop()()
        self.assertFalse(payload.exists())
        self.assertFalse(fixture_database.exists())
        for retained in [junit, terminal, unknown]:
            self.assertTrue(retained.exists())
        # A discarded Actions runner seals success but registers no exit retirement.
        actions_run = self.repo / ".tmp/actions-run"
        runs.begin_temp_run(
            actions_run,
            repo_root=self.repo,
            disposable_class="isolated_test_fixture",
            purpose="actions hook fixture",
            min_free_bytes=0,
        )
        kept = actions_run / "payload.bin"
        kept.write_bytes(b"actions payload")
        namespace["_test_db_dir"] = actions_run
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            sessionfinish(None, 0)
        self.assertEqual(callbacks, [])
        self.assertTrue(kept.exists())
        self.assertEqual(
            json.loads((actions_run / runs.MANIFEST_NAME).read_text())["status"], "completed"
        )
        for status in (1, 2):
            second = self.repo / f".tmp/failed-run-{status}"
            runs.begin_temp_run(
                second,
                repo_root=self.repo,
                disposable_class="isolated_test_fixture",
                purpose="failed hook fixture",
                min_free_bytes=0,
            )
            retained = second / "payload.bin"
            retained.write_bytes(b"failed reproduction")
            namespace["_test_db_dir"] = second
            sessionfinish(None, status)
            self.assertEqual(callbacks, [])
            self.assertTrue(retained.exists())
            self.assertEqual(
                json.loads((second / runs.MANIFEST_NAME).read_text())["status"],
                "failed",
            )


if __name__ == "__main__":
    unittest.main()

"""Hermetic tests for the typed roadmap-reconciliation producer."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import cast

import pytest

import execution.reconcile_quality_baseline as cli_module
from quality.architecture import ArchitectureMetrics, ArchitectureReceipt, LineCounts, ModuleMetric
from quality.duplicates import DuplicateInventory, DuplicateTotals
from quality.reachability import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    InputManifestEntry,
    ReachabilityGraph,
)
from quality.roadmap_reconciliation import (
    SOURCE_PATHS,
    CurrentReceipts,
    ReconciliationReceipt,
    SourceKey,
    claim_manifest_hash,
    reconcile_staged_subject,
    reconcile_with_receipts_for_testing,
    roadmap_facts,
)
from quality.static_quality import DiagnosticSummary, RuntimeIdentity, StaticQualityInventory
from quality.test_db_patterns import BuilderClassification
from quality.test_db_patterns import Evidence as TestDbEvidence
from quality.test_db_patterns import TestDbAudit as DbAudit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "execution"))


def _load_json_object(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    out: dict[str, object] = {}
    for k, v in cast(dict[object, object], loaded).items():
        assert isinstance(k, str)
        out[k] = v
    return out


def _mutable_nested(path: Path, key: str) -> tuple[dict[str, object], dict[str, object]]:
    top = _load_json_object(path)
    nested = top.get(key)
    assert isinstance(nested, dict)
    inner: dict[str, object] = {}
    for k, v in cast(dict[object, object], nested).items():
        assert isinstance(k, str)
        inner[k] = v
    return top, inner


def _arch(nmod: int = 1291, loc: int = 554615, scc: int = 16, big: int = 24) -> ArchitectureReceipt:
    mods = tuple(
        ModuleMetric(
            path=f"src/m{i}.py",
            module=f"m{i}",
            lines=LineCounts(physical=10, nonblank=8, noncomment=8),
            internal_fan_out=1,
            responsibilities=(),
            public_functions=1,
            fully_annotated_public_functions=1,
        )
        for i in range(2)
    )
    metrics = ArchitectureMetrics(
        executable_modules=nmod,
        total_noncomment_loc=loc,
        modules_over_1000_loc=119,
        modules_over_2000_loc=26,
        modules_at_least_3000_loc=7,
        max_internal_fan_out=5,
        scc_count=scc,
        scc_module_count=77,
        largest_scc=big,
        composition_root_loc={},
        composition_root_fan_out={},
        facade_violations=(),
        modules=mods,
        strongly_connected_components=(),
    )
    return ArchitectureReceipt(
        scoped_revision="WORKTREE",
        scoped_commit="a" * 40,
        scanner_sha256="b" * 64,
        source_sha256="c" * 64,
        python_version="3.11.0",
        ast_version="python-3.11",
        definitions={"k": "v"},
        metrics=metrics,
    )


def _dup(groups: int = 140, funcs: int = 397) -> DuplicateInventory:
    t = DuplicateTotals(groups=groups, participating_functions=funcs, duplicated_loc=100)
    n = DuplicateTotals(groups=0, participating_functions=0, duplicated_loc=0)
    return DuplicateInventory(
        scoped_revision="WORKTREE",
        commit_hash="a" * 40,
        source_hash="d" * 64,
        scanner_hash="e" * 64,
        parser_version="python-ast-normalized-v1",
        python_version="3.11.0",
        thresholds={"min_ast_nodes": 20, "min_body_lines": 15},
        files_scanned=5,
        functions_scanned=10,
        exact_groups=[],
        near_miss_groups=[],
        exact_totals=t,
        near_miss_totals=n,
        definitions={"k": "v"},
        parse_errors=[],
    )


def _static(ruff: int = 2, pyright: int = 27924) -> StaticQualityInventory:
    def diag(tool: str, count: int) -> DiagnosticSummary:
        raw = f"{tool}:{count}".encode()
        return DiagnosticSummary(
            tool=tool,
            command=[tool],
            version="1",
            exit_status=0,
            count=count,
            receipt_path=".tmp/x.json",
            command_hash="f" * 64,
            version_hash="f" * 64,
            receipt_sha256=hashlib.sha256(raw).hexdigest(),
            receipt_bytes=len(raw),
            receipt_base64=base64.b64encode(raw).decode("ascii"),
        )

    return StaticQualityInventory(
        repo_root=".",
        tracked_python_files=3,
        active=["a.py"],
        immutable_historical_migration=[],
        generated_declarative_exception=[],
        diagnostics=[
            diag("ruff", ruff),
            diag("ruff-format", 0),
            diag("pyright", pyright),
            diag("source-ignore-comments", 0),
        ],
        current_exclusions={},
        status="PASS",
        violations=[],
        scoped_commit="a" * 40,
        source_hash="1" * 64,
        config_hash="2" * 64,
        receipt_identity="abc",
        runtime=RuntimeIdentity(
            implementation="CPython",
            python_version="3.11.0",
            platform="linux",
            machine="x86_64",
        ),
        suppressions_by_file={},
    )


def _evidence(value: TestDbEvidence) -> tuple[TestDbEvidence, ...]:
    return (value,)


def _db(nfiles: int = 5, up: int = 1, mig: int = 1, ddl: int = 1) -> DbAudit:
    builders: list[BuilderClassification] = []
    files = [f"tests/test_{i}.py" for i in range(nfiles)]
    upgrade_evidence: TestDbEvidence = "call:upgrade"
    migrated_evidence: TestDbEvidence = "call:migrated_db"
    ddl_evidence: TestDbEvidence = "sql:create table"
    evidence_sets: list[tuple[TestDbEvidence, ...]] = (
        [_evidence(upgrade_evidence)] * up
        + [_evidence(migrated_evidence)] * mig
        + [_evidence(ddl_evidence)] * ddl
    )
    for i, evidence in enumerate(evidence_sets):
        builders.append(
            BuilderClassification(
                path=f"tests/test_b{i}.py",
                taxonomy="custom-bootstrap",
                evidence=evidence,
            )
        )
    return DbAudit(
        scoped_commit="a" * 40,
        scanner_sha256="1" * 64,
        source_sha256="2" * 64,
        collection_status="COMPLETE",
        raw_audit_status="PASS",
        tracked_test_files=tuple(files),
        database_builders=tuple(builders),
        counts_by_taxonomy={},
        findings=(),
        violations=(),
    )


def _graph(
    edge_target: str | None = "src/synthesis/theme_synth.py",
    *,
    source: str = "execution/a.py",
    kind: EdgeKind = "import",
) -> ReachabilityGraph:
    edges = (
        []
        if edge_target is None
        else [
            GraphEdge(
                source=source,
                target=edge_target,
                kind=kind,
                evidence="import x",
                confidence="high",
                line=3,
                unknown=False,
            )
        ]
    )
    manifest: tuple[InputManifestEntry, ...] = (
        InputManifestEntry(path=source, content_sha256="1" * 64),
    )
    return ReachabilityGraph(
        subject_commit="a" * 40,
        source_manifest_sha256="1" * 64,
        scanner_sha256="2" * 64,
        scanner_version="1.2.1",
        python_version="3.11",
        population=(source,),
        exclusions=(),
        attempted_input_manifest=manifest,
        collection_status="COMPLETE",
        closure_status="PASS",
        parser={
            "name": "ast+xml+literal-scanner",
            "version": "1.2.1",
            "python": ">=3.11",
            "source_sha256": "3" * 64,
        },
        nodes=[GraphNode(id=source, kind="python")],
        edges=edges,
        roots=[source],
        unresolved=[],
        diagnostics=[],
        unknown_edges=[],
        hold=False,
        stats={},
    )


def _current(
    *,
    architecture: ArchitectureReceipt | None = None,
    duplicates: DuplicateInventory | None = None,
    static: StaticQualityInventory | None = None,
    test_db: DbAudit | None = None,
    reachability: ReachabilityGraph | None = None,
) -> CurrentReceipts:
    return CurrentReceipts(
        architecture=architecture or _arch(),
        duplicates=duplicates or _dup(),
        static=static or _static(),
        test_db=test_db or _db(),
        reachability=reachability or _graph(),
    )


ROADMAP_LINES = {
    "production module count": "production modules: 1291",
    "production noncomment LOC": "noncomment LOC: 554615",
    "scc count": "scc count: 16",
    "largest scc": "largest scc: 24",
    "exact duplicate groups": "exact duplicate groups: 140",
    "exact duplicate functions": "exact duplicate functions: 397",
    "ruff diagnostics": "ruff diagnostics: 2",
    "pyright diagnostics": "pyright diagnostics: 27924",
    "test files": "test files: 1092",
    "upgrade builders": "upgrade builders: 172",
    "migrated builders": "migrated builders: 146",
    "ddl builders": "ddl builders: 550",
    "theme live edge": "theme_synth live edge: true",
    "refetch absence": "refetch_aggregator absent: true",
    "full suite seconds": "full suite seconds: 1046.92",
    "unreachable scripts": "unreachable scripts: 85",
}


def _seed(tmp_path: Path, cur: CurrentReceipts) -> None:
    mapping = {
        "architecture": cur.architecture,
        "duplicates": cur.duplicates,
        "static": cur.static,
        "test_db": cur.test_db,
        "reachability": cur.reachability,
    }
    for key, rel in SOURCE_PATHS.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(mapping[key].model_dump_json(indent=2) + "\n", encoding="utf-8")
    rm = tmp_path / "docs/quality/quality-9plus-roadmap.md"
    rm.parent.mkdir(parents=True, exist_ok=True)
    rm.write_text(
        "\n".join(ROADMAP_LINES[f.name] for f in roadmap_facts()) + "\n", encoding="utf-8"
    )


def test_unsupported_rejected_unscored(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    res = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert res.status == "PASS"
    for name in ("full suite seconds", "unreachable scripts"):
        c = next(x for x in res.claims if x.name == name)
        assert c.verdict == "rejected" and not c.scored_eligible and c.observed is None
    assert res.scored_claims + res.rejected_claims == len(res.claims)


def test_typed_corrections(tmp_path: Path) -> None:
    cur = _current(architecture=_arch(nmod=1300))
    _seed(tmp_path, cur)
    res = reconcile_with_receipts_for_testing(tmp_path, cur)
    c = next(x for x in res.claims if x.name == "production module count")
    assert c.verdict == "corrected" and c.observed == 1300 and c.scored_eligible
    assert c.evidence is not None and c.evidence.path == SOURCE_PATHS["architecture"]


def test_missing_source_holds(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    (tmp_path / SOURCE_PATHS["test_db"]).unlink()
    res = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert res.status == "HOLD"
    c = next(x for x in res.claims if x.name == "test files")
    assert c.verdict == "rejected" and not c.scored_eligible


def test_malformed_source_holds(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    (tmp_path / SOURCE_PATHS["architecture"]).write_text("{bad", encoding="utf-8")
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "HOLD"


def test_wrong_schema_holds(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    p = tmp_path / SOURCE_PATHS["duplicates"]
    d = _load_json_object(p)
    d["schema_version"] = "wrong"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "HOLD"


def test_wrong_status_holds(tmp_path: Path) -> None:
    for key, rel in (
        ("static", SOURCE_PATHS["static"]),
        ("reachability", SOURCE_PATHS["reachability"]),
    ):
        cur = _current()

        d = tmp_path / key
        d.mkdir(parents=True, exist_ok=True)
        _seed(d, cur)
        p = d / rel
        payload = _load_json_object(p)
        if key == "static":
            payload["status"] = "HOLD"
        else:
            payload["closure_status"] = "HOLD"
            payload["hold"] = True
        p.write_text(json.dumps(payload), encoding="utf-8")
        assert reconcile_with_receipts_for_testing(d, cur).status == "HOLD"


def test_tampered_source_holds(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    p = tmp_path / SOURCE_PATHS["duplicates"]
    top, nested = _mutable_nested(p, "exact_totals")
    nested["groups"] = 999
    top["exact_totals"] = nested
    p.write_text(json.dumps(top), encoding="utf-8")
    res = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert res.status == "HOLD"
    assert any("duplicates-ratchet" in v for v in res.violations)


def test_fresh_reproduction_and_volatile_only(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    p = tmp_path / SOURCE_PATHS["architecture"]
    d1 = _load_json_object(p)
    d1["scoped_commit"] = "b" * 40
    p.write_text(json.dumps(d1), encoding="utf-8")
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "PASS"
    top2, metrics = _mutable_nested(p, "metrics")
    metrics["scc_count"] = 99
    top2["metrics"] = metrics
    p.write_text(json.dumps(top2), encoding="utf-8")
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "HOLD"


def test_deterministic_hashes(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    a = reconcile_with_receipts_for_testing(tmp_path, cur)
    b = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert a.source_hash == b.source_hash
    assert a.claim_manifest_sha256 == claim_manifest_hash() == b.claim_manifest_sha256
    assert [c.name for c in a.claims] == [c.name for c in b.claims]
    assert len(a.source_hash) == 64
    assert len(a.claim_manifest_sha256) == 64


def test_roadmap_omission_tamper_ambiguous_utf8(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    (tmp_path / "docs/quality/quality-9plus-roadmap.md").unlink()
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "HOLD"
    _seed(tmp_path, cur)
    rm = tmp_path / "docs/quality/quality-9plus-roadmap.md"
    rm.write_text(rm.read_text(encoding="utf-8").replace("1291", "1292"), encoding="utf-8")
    r = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert r.status == "HOLD" and not any(c.scored_eligible for c in r.claims)
    _seed(tmp_path, cur)
    with open(rm, "ab") as fh:
        fh.write(b"\nproduction modules: 1291\n")
    assert any(
        "ambiguous" in v for v in reconcile_with_receipts_for_testing(tmp_path, cur).violations
    )
    rm.write_bytes(b"\xff\xfe bad")
    r2 = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert r2.status == "HOLD" and any("UTF-8" in v for v in r2.violations)


def test_claim_content_validation(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    res = reconcile_with_receipts_for_testing(tmp_path, cur)
    for c in res.claims:
        if c.verdict == "rejected":
            assert not c.scored_eligible
        else:
            assert c.scored_eligible and c.observed is not None and c.evidence is not None
    assert res.scored_claims == sum(1 for c in res.claims if c.scored_eligible)
    assert res.rejected_claims == sum(1 for c in res.claims if c.verdict == "rejected")


def test_named_audit_corrections(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    res = reconcile_with_receipts_for_testing(tmp_path, cur)
    names = {"theme live edge", "refetch absence", "upgrade builders"}
    got = {c.name: c for c in res.claims if c.name in names}
    assert set(got) == names
    assert all(c.observed is True or c.observed == 1 for c in got.values())
    assert all(c.scored_eligible for c in got.values())


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("tests/test_theme.py", "import"),
        ("instruction_tests/test_theme.py", "import"),
        ("execution/reconstruction_manifest.py", "reconstruction"),
        ("execution/directive_loader.py", "directive"),
        ("docs/theme_notes.md", "import"),
    ],
)
def test_theme_live_edge_requires_runtime_source(
    tmp_path: Path, source: str, kind: EdgeKind
) -> None:
    current = _current(reachability=_graph(source=source, kind=kind))
    _seed(tmp_path, current)
    receipt = reconcile_with_receipts_for_testing(tmp_path, current)
    claim = next(item for item in receipt.claims if item.name == "theme live edge")
    assert receipt.status == "PASS"
    assert claim.observed is False
    assert claim.verdict == "corrected"
    assert claim.scored_eligible


def test_exact_value_tamper(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    rm = tmp_path / "docs/quality/quality-9plus-roadmap.md"
    text = rm.read_text(encoding="utf-8")
    rm.write_text(text.replace("production modules: 1291", "production modules: 12912"))
    r1 = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert r1.status == "HOLD"
    assert not any(c.scored_eligible for c in r1.claims)
    assert all(c.provisional_evidence is None for c in r1.claims)
    assert r1.roadmap_source is None
    _seed(tmp_path, cur)
    text = rm.read_text(encoding="utf-8")
    rm.write_text(text.replace("scc count: 16", "scc count: 160"))
    r2 = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert r2.status == "HOLD"
    assert not any(c.scored_eligible for c in r2.claims)
    assert all(c.provisional_evidence is None for c in r2.claims)
    _seed(tmp_path, cur)
    text = rm.read_text(encoding="utf-8")
    rm.write_text(text.replace("theme_synth live edge: true", "theme_synth live edge: trueish"))
    r3 = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert r3.status == "HOLD"
    assert not any(c.scored_eligible for c in r3.claims)
    assert all(c.provisional_evidence is None for c in r3.claims)
    assert r3.roadmap_source is None


def test_duplicate_receipt_keys_hold(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    p = tmp_path / SOURCE_PATHS["duplicates"]
    text = p.read_text(encoding="utf-8")
    assert "140" in text
    tampered = text.replace('"groups": 140', '"groups": 999, "groups": 140', 1)
    assert tampered != text
    p.write_text(tampered, encoding="utf-8")
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "HOLD"


def test_evidence_shape(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    good = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert good.status == "PASS"
    assert good.roadmap_source is not None
    assert len(good.roadmap_source.sha256) == 64
    assert good.roadmap_source.sha256 != "0" * 64
    for c in good.claims:
        if c.scored_eligible:
            assert c.evidence is not None
            assert c.provisional_evidence is not None
            assert c.provisional_evidence.sha256 != "0" * 64
            assert c.provisional_evidence.locator != "line 0"
        elif c.provisional_evidence is not None:
            assert c.provisional_evidence.sha256 != "0" * 64
            assert c.provisional_evidence.locator != "line 0"
    (tmp_path / "docs/quality/quality-9plus-roadmap.md").unlink()
    bad = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert bad.status == "HOLD"
    assert bad.roadmap_source is None
    assert all(c.provisional_evidence is None for c in bad.claims)
    for c in bad.claims:
        if c.provisional_evidence is not None:
            assert c.provisional_evidence.sha256 != "0" * 64


def test_reconcile_uses_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    seen: dict[str, Path] = {}

    def fake_fresh(root: Path) -> CurrentReceipts:
        seen["root"] = root
        return cur

    def clean_state(_root: Path) -> tuple[str | None, bool | None]:
        return "a" * 40, False

    monkeypatch.setattr("quality.roadmap_reconciliation._fresh_receipts", fake_fresh)
    import quality.roadmap_reconciliation as rr

    monkeypatch.setattr(rr, "_git_state", clean_state)
    res = rr.reconcile(tmp_path)
    assert seen["root"] == tmp_path.resolve()
    assert res.status == "PASS"


def test_cli_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    good = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert good.status == "PASS"

    def return_good(_root: Path) -> ReconciliationReceipt:
        return good

    monkeypatch.setattr(cli_module, "reconcile", return_good)
    assert cli_module.main(["--root", str(tmp_path)]) == 0
    capsys.readouterr()
    hold = good.model_copy(update={"status": "HOLD"})

    def return_hold(_root: Path) -> ReconciliationReceipt:
        return hold

    monkeypatch.setattr(cli_module, "reconcile", return_hold)
    assert cli_module.main(["--root", str(tmp_path)]) == 2
    capsys.readouterr()
    assert cli_module.main(["--root", str(tmp_path / "nope")]) == 1


def test_tmp_only_and_symlink_hold(tmp_path: Path) -> None:
    cur = _current()
    only = tmp_path / "only"
    only.mkdir()
    _seed(only, cur)
    (only / "docs/quality/quality-9plus-roadmap.md").unlink()
    fallback = only / ".tmp/quality-9plus-roadmap.md"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text(
        "\n".join(ROADMAP_LINES[f.name] for f in roadmap_facts()) + "\n", encoding="utf-8"
    )
    res = reconcile_with_receipts_for_testing(only, cur)
    assert res.status == "HOLD"
    assert res.roadmap_source is None
    assert all(c.provisional_evidence is None for c in res.claims)
    linked = tmp_path / "linked"
    linked.mkdir()
    _seed(linked, cur)
    canonical = linked / "docs/quality/quality-9plus-roadmap.md"
    raw = canonical.read_bytes()
    canonical.unlink()
    target = linked / "docs/quality/real-roadmap.md"
    target.write_bytes(raw)
    canonical.symlink_to("real-roadmap.md")
    res2 = reconcile_with_receipts_for_testing(linked, cur)
    assert res2.status == "HOLD"
    assert res2.roadmap_source is None


def test_public_reconcile_binds_clean_stable_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    _seed(tmp_path, cur)

    def fresh(_root: Path) -> CurrentReceipts:
        return cur

    def clean_state(_root: Path) -> tuple[str | None, bool | None]:
        return "a" * 40, False

    monkeypatch.setattr(rr, "_fresh_receipts", fresh)
    monkeypatch.setattr(rr, "_git_state", clean_state)

    result = rr.reconcile(tmp_path)

    assert result.status == "PASS"
    assert result.subject_commit == "a" * 40
    assert result.worktree_dirty is False
    contradictory = result.model_dump(mode="json")
    contradictory["worktree_dirty"] = True
    with pytest.raises(ValueError, match="exact clean Git subject"):
        ReconciliationReceipt.model_validate(contradictory)


@pytest.mark.parametrize(
    ("states", "message"),
    [
        ((("a" * 40, True), ("a" * 40, False), ("a" * 40, False)), "dirty before collection"),
        ((("a" * 40, False), ("a" * 40, True), ("a" * 40, True)), "worktree is dirty"),
        ((("a" * 40, False), ("b" * 40, False), ("b" * 40, False)), "HEAD changed"),
        ((("a" * 40, False), ("a" * 40, None), ("a" * 40, None)), "identity is unavailable"),
        (((None, None), (None, None), (None, None)), "identity is unavailable"),
        ((("a" * 40, False), ("a" * 40, False), ("a" * 40, True)), "dirty after collection"),
        ((("a" * 40, False), ("a" * 40, False), ("b" * 40, False)), "HEAD changed"),
    ],
)
def test_public_reconcile_rejects_unstable_or_dirty_git_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    states: tuple[
        tuple[str | None, bool | None],
        tuple[str | None, bool | None],
        tuple[str | None, bool | None],
    ],
    message: str,
) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    _seed(tmp_path, cur)
    observations = iter(states)

    def fresh(_root: Path) -> CurrentReceipts:
        return cur

    def next_state(_root: Path) -> tuple[str | None, bool | None]:
        return next(observations)

    monkeypatch.setattr(rr, "_fresh_receipts", fresh)
    monkeypatch.setattr(rr, "_git_state", next_state)

    result = rr.reconcile(tmp_path)

    assert result.status == "HOLD"
    assert any(message in violation for violation in result.violations)


def test_public_reconcile_rejects_changed_ignored_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    _seed(tmp_path, cur)
    observations = 0

    def fresh(_root: Path) -> CurrentReceipts:
        return cur

    def clean_state(_root: Path) -> tuple[str | None, bool | None]:
        nonlocal observations
        observations += 1
        if observations == 3:
            (tmp_path / SOURCE_PATHS["reachability"]).write_text("{}\n", encoding="utf-8")
        return "a" * 40, False

    monkeypatch.setattr(rr, "_fresh_receipts", fresh)
    monkeypatch.setattr(rr, "_git_state", clean_state)

    result = rr.reconcile(tmp_path)

    assert result.status == "HOLD"
    assert any("ignored source receipt changed" in value for value in result.violations)


@pytest.mark.parametrize(
    ("source", "field", "volatile_value"),
    [
        ("static", "scoped_commit", "b" * 40),
        ("static", "repo_root", "volatile-root"),
        ("static", "receipt_identity", "volatile-identity"),
        ("duplicates", "commit_hash", "b" * 40),
        ("duplicates", "scoped_revision", "VOLATILE"),
        ("test_db", "scoped_commit", "b" * 40),
        ("reachability", "subject_commit", "b" * 40),
    ],
)
def test_volatile_only_equivalence(
    tmp_path: Path, source: SourceKey, field: str, volatile_value: object
) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    path = tmp_path / SOURCE_PATHS[source]
    payload = _load_json_object(path)
    payload[field] = volatile_value
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "PASS"


@pytest.mark.parametrize(
    ("source", "field", "bad_value"),
    [
        ("duplicates", "parse_errors", ["boom"]),
        ("static", "violations", ["boom"]),
        ("test_db", "violations", ["boom"]),
        ("reachability", "hold", True),
        ("reachability", "closure_status", "HOLD"),
    ],
)
def test_inadmissible_receipts_hold(
    tmp_path: Path, source: SourceKey, field: str, bad_value: object
) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    path = tmp_path / SOURCE_PATHS[source]
    payload = _load_json_object(path)
    payload[field] = bad_value
    if source == "reachability" and field == "hold":
        payload["closure_status"] = "HOLD"
    if source == "reachability" and field == "closure_status":
        payload["hold"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert reconcile_with_receipts_for_testing(tmp_path, cur).status == "HOLD"


def test_parent_symlink_escape_holds(tmp_path: Path) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    quality_dir = tmp_path / ".tmp/quality"
    original = quality_dir / "reachability-check.json"
    raw = original.read_bytes()
    external = tmp_path / "_external_quality"
    external.mkdir(parents=True, exist_ok=True)
    (external / "reachability-check.json").write_bytes(raw)
    original.unlink()
    quality_dir.rmdir()
    quality_dir.symlink_to(external, target_is_directory=True)
    res = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert res.status == "HOLD"
    assert any("reachability-check" in v for v in res.violations)


def test_cli_output_collision_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    target = tmp_path / SOURCE_PATHS["reachability"]
    before = target.read_bytes()
    good = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert good.status == "PASS"
    called = {"hit": False}

    def fake_reconcile(_root: Path) -> ReconciliationReceipt:
        called["hit"] = True
        return good

    monkeypatch.setattr(cli_module, "reconcile", fake_reconcile)
    rc = cli_module.main(["--root", str(tmp_path), "--output", str(target)])
    assert rc == 1
    assert called["hit"] is False
    assert target.read_bytes() == before
    captured = capsys.readouterr()
    assert "output_aliases_protected_input" in captured.err


def test_cli_output_hardlink_alias_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    target = tmp_path / SOURCE_PATHS["reachability"]
    before = target.read_bytes()
    good = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert good.status == "PASS"
    alias = tmp_path / "hardlink-alias.json"
    alias.hardlink_to(target)
    called = {"hit": False}

    def fake_reconcile(_root: Path) -> ReconciliationReceipt:
        called["hit"] = True
        return good

    monkeypatch.setattr(cli_module, "reconcile", fake_reconcile)
    rc = cli_module.main(["--root", str(tmp_path), "--output", str(alias)])
    assert rc == 1
    assert called["hit"] is False
    assert target.read_bytes() == before
    captured = capsys.readouterr()
    assert "output_aliases_protected_input" in captured.err


def test_cli_output_late_swap_is_replaced_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quality.atomic_write as atomic_write

    cur = _current()
    _seed(tmp_path, cur)
    protected = tmp_path / SOURCE_PATHS["reachability"]
    before = protected.read_bytes()
    output = tmp_path / ".tmp/reconciled.json"
    good = reconcile_with_receipts_for_testing(tmp_path, cur)
    assert good.status == "PASS"

    def return_good(_root: Path) -> ReconciliationReceipt:
        return good

    monkeypatch.setattr(cli_module, "reconcile", return_good)
    real_replace = os.replace

    def raced_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == output:
            Path(dst).unlink(missing_ok=True)
            os.link(protected, dst)
        real_replace(src, dst)

    monkeypatch.setattr(atomic_write.os, "replace", raced_replace)

    assert cli_module.main(["--root", str(tmp_path), "--output", str(output)]) == 0
    assert protected.read_bytes() == before
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "PASS"
    assert not os.path.samefile(protected, output)


def _write_staged_manifest(staged_dir: Path, cur: CurrentReceipts, roadmap_text: str) -> Path:
    import hashlib as _hashlib

    staged_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {
        "architecture": cur.architecture.model_dump_json(indent=2) + "\n",
        "duplicates": cur.duplicates.model_dump_json(indent=2) + "\n",
        "static": cur.static.model_dump_json(indent=2) + "\n",
        "test_db": cur.test_db.model_dump_json(indent=2) + "\n",
        "reachability": cur.reachability.model_dump_json(indent=2) + "\n",
        "roadmap": roadmap_text,
    }
    filenames = {
        "architecture": "architecture.json",
        "duplicates": "duplicates.json",
        "static": "static.json",
        "test_db": "test_db.json",
        "reachability": "reachability.json",
        "roadmap": "roadmap.md",
    }
    manifest: dict[str, dict[str, str]] = {}
    for key, payload in mapping.items():
        target = staged_dir / filenames[key]
        target.write_text(payload, encoding="utf-8")
        digest = _hashlib.sha256(target.read_bytes()).hexdigest()
        manifest[key] = {"path": filenames[key], "sha256": digest}
    manifest_path = staged_dir / "staged-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def _roadmap_text() -> str:
    return "\n".join(ROADMAP_LINES[f.name] for f in roadmap_facts()) + "\n"


def _staged_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cur: CurrentReceipts
) -> tuple[Path, Path]:
    subject = tmp_path / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "staged"
    manifest = _write_staged_manifest(staged, cur, _roadmap_text())

    def fake_fresh(root: Path) -> CurrentReceipts:
        assert root == subject.resolve()
        return cur

    def clean_state(_root: Path) -> tuple[str | None, bool | None]:
        return "a" * 40, False

    import quality.roadmap_reconciliation as rr

    monkeypatch.setattr(rr, "_fresh_receipts", fake_fresh)
    monkeypatch.setattr(rr, "_git_state", clean_state)
    return subject, manifest


def test_staged_valid_separate_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    res = reconcile_staged_subject(subject, manifest)
    assert res.status == "PASS"
    assert res.subject_commit == "a" * 40
    assert res.worktree_dirty is False
    assert res.roadmap_source is not None


def test_staged_never_uses_testing_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import quality.roadmap_reconciliation as rr

    assert rr.reconcile_staged_subject is not rr.reconcile_with_receipts_for_testing
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    res = reconcile_staged_subject(subject, manifest)
    assert res.status == "PASS"


def test_staged_hash_mismatch_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    payload: dict[str, dict[str, str]] = json.loads(manifest.read_text(encoding="utf-8"))
    assert isinstance(payload["static"]["sha256"], str)
    payload["static"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_roadmap_hash_mismatch_rejects_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    target = tmp_path / "staged" / "roadmap.md"
    target.write_text(target.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    res = reconcile_staged_subject(subject, manifest)
    assert res.status == "HOLD"
    assert any("inadmissible staged roadmap: hash mismatch" in v for v in res.violations)
    assert res.roadmap_source is None
    assert all(c.verdict == "rejected" for c in res.claims)
    assert not any(c.scored_eligible for c in res.claims)
    assert all(c.provisional_evidence is None for c in res.claims)


def test_staged_schema_and_status_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    import hashlib as _hashlib

    target = tmp_path / "staged" / "static.json"
    bad = json.loads(target.read_text(encoding="utf-8"))
    bad["status"] = "HOLD"
    target.write_text(json.dumps(bad), encoding="utf-8")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["static"]["sha256"] = _hashlib.sha256(target.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_manifest_shape_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["roadmap"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"
    manifest.write_text(
        '{"architecture": {"path": "x", "sha256": "' + "0" * 64 + '"}}', encoding="utf-8"
    )
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_duplicate_keys_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    raw = manifest.read_text(encoding="utf-8")
    dup = raw.replace(
        '"architecture"',
        '"architecture", "architecture": {"path": "x", "sha256": "'
        + "0" * 64
        + '"}, "architecture"',
        1,
    )
    manifest.write_text(dup, encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_duplicate_paths_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    import hashlib as _hashlib

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["duplicates"]["path"] = payload["architecture"]["path"]
    payload["duplicates"]["sha256"] = payload["architecture"]["sha256"]
    _ = _hashlib.sha256(b"x").hexdigest()
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_path_escape_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["roadmap"]["path"] = "../escape.md"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"
    payload2 = json.loads(manifest.read_text(encoding="utf-8"))
    payload2["roadmap"]["path"] = "/abs/path.md"
    manifest.write_text(json.dumps(payload2), encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_commit_mismatch_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    import hashlib as _hashlib

    target = tmp_path / "staged" / "architecture.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["scoped_commit"] = "b" * 40
    target.write_text(json.dumps(payload), encoding="utf-8")
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["architecture"]["sha256"] = _hashlib.sha256(target.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8")
    res = reconcile_staged_subject(subject, manifest)
    assert res.status == "HOLD"
    assert any("commit" in v for v in res.violations)


def test_staged_mixed_subject_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _current(architecture=_arch(nmod=1300))
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, other)
    import quality.roadmap_reconciliation as rr

    def fake_fresh(_root: Path) -> CurrentReceipts:
        return cur

    monkeypatch.setattr(rr, "_fresh_receipts", fake_fresh)
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_dirty_subject_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    subject = tmp_path / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "staged"
    manifest = _write_staged_manifest(staged, cur, _roadmap_text())

    def fake_fresh(_root: Path) -> CurrentReceipts:
        return cur

    def fake_git_state(_root: Path) -> tuple[str, bool]:
        return ("a" * 40, True)

    monkeypatch.setattr(rr, "_fresh_receipts", fake_fresh)
    monkeypatch.setattr(rr, "_git_state", fake_git_state)
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_changing_subject_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    subject = tmp_path / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "staged"
    manifest = _write_staged_manifest(staged, cur, _roadmap_text())

    def fake_fresh(_root: Path) -> CurrentReceipts:
        return cur

    monkeypatch.setattr(rr, "_fresh_receipts", fake_fresh)
    states = iter([("a" * 40, False), ("a" * 40, False), ("b" * 40, False)])

    def fake_git_state(_root: Path) -> tuple[str, bool]:
        return next(states)

    monkeypatch.setattr(rr, "_git_state", fake_git_state)
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_symlink_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    target = tmp_path / "staged" / "static.json"
    raw = target.read_bytes()
    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    target.symlink_to(outside)
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_hardlink_duplicate_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _current()
    subject, manifest = _staged_pass(tmp_path, monkeypatch, cur)
    import hashlib as _hashlib

    arch = tmp_path / "staged" / "architecture.json"
    dup = tmp_path / "staged" / "duplicates.json"
    dup.unlink()
    os.link(arch, dup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["duplicates"]["sha256"] = _hashlib.sha256(dup.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_replacement_race_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    subject = tmp_path / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "staged"
    manifest = _write_staged_manifest(staged, cur, _roadmap_text())

    def racing_fresh(root: Path) -> CurrentReceipts:
        (staged / "reachability.json").write_text("{}\n", encoding="utf-8")
        return cur

    monkeypatch.setattr(rr, "_fresh_receipts", racing_fresh)

    def fake_git_state(_root: Path) -> tuple[str, bool]:
        return ("a" * 40, False)

    monkeypatch.setattr(rr, "_git_state", fake_git_state)
    assert reconcile_staged_subject(subject, manifest).status == "HOLD"


def test_staged_cli_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    subject = tmp_path / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "staged"
    manifest = _write_staged_manifest(staged, cur, _roadmap_text())

    def fake_fresh(_root: Path) -> CurrentReceipts:
        return cur

    def fake_git_state(_root: Path) -> tuple[str, bool]:
        return ("a" * 40, False)

    monkeypatch.setattr(rr, "_fresh_receipts", fake_fresh)
    monkeypatch.setattr(rr, "_git_state", fake_git_state)
    assert (
        cli_module.main(["--subject-root", str(subject), "--staged-manifest", str(manifest)]) == 0
    )
    capsys.readouterr()
    assert cli_module.main(["--subject-root", str(subject)]) == 1
    capsys.readouterr()
    assert (
        cli_module.main(
            [
                "--repo-root",
                str(subject),
                "--subject-root",
                str(subject),
                "--staged-manifest",
                str(manifest),
            ]
        )
        == 1
    )
    capsys.readouterr()


def test_staged_cli_output_alias_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import quality.roadmap_reconciliation as rr

    cur = _current()
    subject = tmp_path / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "staged"
    manifest = _write_staged_manifest(staged, cur, _roadmap_text())

    def fake_fresh(_root: Path) -> CurrentReceipts:
        return cur

    def fake_git_state(_root: Path) -> tuple[str, bool]:
        return ("a" * 40, False)

    monkeypatch.setattr(rr, "_fresh_receipts", fake_fresh)
    monkeypatch.setattr(rr, "_git_state", fake_git_state)
    staged_input = staged / "architecture.json"
    rc = cli_module.main(
        [
            "--subject-root",
            str(subject),
            "--staged-manifest",
            str(manifest),
            "--output",
            str(staged_input),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "output_aliases_protected_input" in captured.err
    alias = tmp_path / "alias.json"
    alias.hardlink_to(staged_input)
    rc2 = cli_module.main(
        ["--subject-root", str(subject), "--staged-manifest", str(manifest), "--output", str(alias)]
    )
    assert rc2 == 1
    capsys.readouterr()


def test_legacy_repo_root_still_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cur = _current()
    _seed(tmp_path, cur)
    good = reconcile_with_receipts_for_testing(tmp_path, cur)

    def fake_reconcile(_root: Path) -> ReconciliationReceipt:
        return good

    monkeypatch.setattr(cli_module, "reconcile", fake_reconcile)
    assert cli_module.main(["--repo-root", str(tmp_path)]) == 0
    capsys.readouterr()

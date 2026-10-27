"""Bounded native source inputs for the retained transcript evaluator."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from evals.ask_citations import (
    document_narrative_eval_records,
    native_transcript_qualification_limits,
    run_document_narrative_eval,
)
from evals.harness import read_transcript_audit_proof


def _canonical_sha(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _native_source(version: str, number: int) -> tuple[dict[str, object], str]:
    text = f"{version}: " + (f"source segment {number}. " * 300)
    manifest: dict[str, object] = {
        "document_version_id": version,
        "issuer_id": f"issuer-{number}",
        "ticker": f"T{number}",
        "document_type": "transcript",
        "source_ref": f"/source/{100 + number}",
        "blob_sha256": hashlib.sha256(f"blob-{number}".encode()).hexdigest(),
        "raw_byte_size": 1000 + number,
        "binding_kind": "canonical",
        "recorded_issuer_id": f"issuer-{number}",
        "binding_revision_id": f"binding-{number}",
        "period_start": "2026-01-01",
        "period_end": f"2026-03-{number:02d}",
        "fiscal_year": 2026,
        "fiscal_quarter": number,
        "fiscal_selection_request": {
            "document_version_id": version,
            "fiscal_year": None,
            "fiscal_quarter": None,
        },
        "fiscal_selection_proof": {"request": {"version": version}},
        "extractor": {
            "name": "approved-transcript-text",
            "code_version": "v1",
            "config_sha256": hashlib.sha256(b"extractor-config").hexdigest(),
            "approved_extraction_run_id": f"extract-{number}",
            "selection_cutoff": None,
        },
        "fulltext_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "consumed_fulltext_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "fulltext_characters": len(text),
        "prompt_text_limit": None,
    }
    return manifest, text


def _corpus_files(
    root: Path,
) -> tuple[Path, Path, Path, Path, dict[str, tuple[dict[str, object], str]]]:
    versions = [f"document-version-{number}" for number in range(1, 4)]
    source_values = {
        version: _native_source(version, number) for number, version in enumerate(versions, start=1)
    }
    cases: list[dict[str, object]] = []
    case_ids: list[str] = []
    for number, version in enumerate(versions, start=1):
        for variant in range(3):
            case_id = f"native-{number}-{variant}"
            case_ids.append(case_id)
            positive_words = (850, 870, 899)[number - 1]
            words = [f"claim{index}" for index in range(positive_words)]
            quote_parts = [" ".join(words[offset::3]) for offset in range(3)]
            lines: list[dict[str, object]] = [
                {
                    "quote": quote_parts[index] if variant == 0 else f"{version} claim {index}",
                    "supported": not (variant > 0 and index == variant),
                    "kind": "reported_fact",
                    "context": {"period": "FY2026 Q1"},
                }
                for index in range(3)
            ]
            cases.append(
                {
                    "case_id": case_id,
                    "document_version_id": version,
                    "lines": lines,
                }
            )
    gold = {"schema_version": "transcript-summary-native-gold@1", "cases": cases}
    gold_bytes = json.dumps(gold, ensure_ascii=False, sort_keys=True).encode("utf-8")
    gold_sha256 = hashlib.sha256(gold_bytes).hexdigest()
    state_root = root / "source-state"
    gold_dir = state_root / "data/model_evals/transcript_summary_qualification/gold"
    gold_dir.mkdir(parents=True)
    (gold_dir / f"{gold_sha256}.json").write_bytes(gold_bytes)

    context: dict[str, object] = {
        "schema_version": "transcript-summary-grounding-context@1",
        "cases": [],
        "native_gold": {
            "path": f"gold/{gold_sha256}.json",
            "sha256": gold_sha256,
            "case_ids": case_ids,
            "sources": [
                {
                    "document_version_id": version,
                    "blob_sha256": manifest["blob_sha256"],
                    "fulltext_sha256": manifest["fulltext_sha256"],
                    "source_manifest_sha256": _canonical_sha(manifest),
                }
                for version, (manifest, _text) in source_values.items()
            ],
        },
    }
    golden_path = root / "base.json"
    context_path = root / "context.json"
    golden_path.write_text(json.dumps({"cases": []}), encoding="utf-8")
    context_path.write_text(json.dumps(context), encoding="utf-8")
    source_db_path = root / "source.db"
    with sqlite3.connect(source_db_path):
        pass
    return golden_path, context_path, source_db_path, state_root, source_values


def _install_resolver(
    monkeypatch: pytest.MonkeyPatch,
    source_values: dict[str, tuple[dict[str, object], str]],
) -> None:
    import pipeline.transcript_summary as transcript_summary

    def resolve(
        conn: sqlite3.Connection, version: str, *, state_root: Path
    ) -> tuple[dict[str, object], str, str]:
        assert state_root.is_dir()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden_source_write (id INTEGER)")
        manifest, text = source_values[version]
        return manifest, text, str(manifest["source_ref"])

    monkeypatch.setattr(transcript_summary, "resolve_transcript_summary_source", resolve)


def _rewrite_private_gold(
    context_path: Path,
    state_root: Path,
    update: Callable[[object], None],
) -> None:
    context = json.loads(context_path.read_text(encoding="utf-8"))
    index = cast("dict[str, object]", context["native_gold"])
    path = state_root / "data/model_evals/transcript_summary_qualification" / str(index["path"])
    gold = json.loads(path.read_text(encoding="utf-8"))
    update(gold)
    payload = json.dumps(gold, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    path.unlink()
    path.with_name(f"{digest}.json").write_bytes(payload)
    index["path"] = f"gold/{digest}.json"
    index["sha256"] = digest
    context_path.write_text(json.dumps(context), encoding="utf-8")


def test_native_records_use_resolved_full_source_and_exact_gold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)

    records, overlimit = document_narrative_eval_records(
        golden,
        context,
        source_db_path=source_db,
        source_state_root=state_root,
    )

    assert overlimit is None
    assert len(records) == 9
    record = records[0]
    assert record[0] == "native-1-0"
    assert len(record[1].split()) == 850
    assert record[2][0].text == sources["document-version-1"][1]
    assert len(record[2][0].text) > 5_000
    assert record[2][0].label == "T1 fiscal Q1 2026"
    assert record[2][0].href == "/source/101"
    assert record[3][0]["context"] == {"period": "FY2026 Q1"}


def test_source_free_synthetic_corpus_remains_standalone(tmp_path: Path) -> None:
    golden = tmp_path / "base.json"
    context = tmp_path / "context.json"
    golden.write_text(json.dumps({"cases": []}), encoding="utf-8")
    context.write_text(json.dumps({"cases": []}), encoding="utf-8")

    records, overlimit = document_narrative_eval_records(golden, context)

    assert records == []
    assert overlimit is None


def test_native_gold_can_remain_in_an_isolated_proof_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    golden, context, source_db, source_state, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)
    proof_state = tmp_path / "isolated-proof-state"
    proof_state.mkdir()
    (source_state / "data").rename(proof_state / "data")
    before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    records, _ = document_narrative_eval_records(
        golden,
        context,
        source_db_path=source_db,
        source_state_root=source_state,
        proof_state_root=proof_state,
    )
    limits = native_transcript_qualification_limits(
        context,
        source_db_path=source_db,
        source_state_root=source_state,
        proof_state_root=proof_state,
    )

    assert len(records) == 9
    positive_items = [
        items
        for _case_id, _body, items, expected in records
        if all(line["supported"] is True for line in expected)
    ]
    assert limits[0] == max(
        sum(len(item.text) + len(item.label) for item in items) for items in positive_items
    )
    from pipeline.transcript_summary_verification import (
        TranscriptAuditQualification,
        TranscriptSummaryVerificationError,
        TranscriptSummaryVerifier,
    )

    # Scope mechanics only; this bypasses issuance and supplies no qualification proof.
    verifier = object.__new__(TranscriptSummaryVerifier)
    verifier.qualification = TranscriptAuditQualification.model_construct(
        max_source_characters=limits[0], max_candidate_characters=limits[1]
    )
    longest = max(positive_items, key=lambda items: sum(len(i.text) + len(i.label) for i in items))
    check_scope = getattr(verifier, "_check_scope")
    check_scope(longest)
    overlimit = [replace(longest[0], text=longest[0].text + "x"), *longest[1:]]
    with pytest.raises(TranscriptSummaryVerificationError, match="full source exceeds"):
        check_scope(overlimit)
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == before
    assert not (source_state / "data").exists()


@pytest.mark.parametrize("location", ["missing", "source", "source_data", "ancestor", "alias"])
def test_native_run_requires_proof_state_outside_live_application_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location: str
) -> None:
    golden, context, source_db, source_state, _sources = _corpus_files(tmp_path)
    proof_root = {
        "missing": None,
        "source": source_state,
        "source_data": source_state / "data" / "candidate",
        "ancestor": tmp_path,
        "alias": tmp_path / "source-alias",
    }[location]
    if location == "alias":
        assert proof_root is not None
        proof_root.symlink_to(source_state, target_is_directory=True)

    def forbidden_budget(_purpose: str, *, db_path: Path) -> None:
        pytest.fail("Invalid proof roots must fail before budget or provider work")

    monkeypatch.setattr("llm_budget.require_explicit_blocking_budget", forbidden_budget)
    with pytest.raises(
        ValueError, match=r"isolated proof_state_root|exclude live application state"
    ):
        run_document_narrative_eval(
            db_path=tmp_path / "isolated-ledger.db",
            golden_path=golden,
            context_golden_path=context,
            code_root=tmp_path,
            source_db_path=source_db,
            source_state_root=source_state,
            proof_state_root=proof_root,
        )


@pytest.mark.parametrize(
    "missing",
    ["source_db_path", "source_state_root"],
)
def test_native_corpus_requires_both_source_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)
    kwargs: dict[str, Path] = {
        "source_db_path": source_db,
        "source_state_root": state_root,
    }
    kwargs.pop(missing)

    with pytest.raises(ValueError, match="requires source_db_path and source_state_root"):
        document_narrative_eval_records(golden, context, **kwargs)


@pytest.mark.parametrize(
    "field",
    [
        "document_version_id",
        "blob_sha256",
        "fulltext_sha256",
        "period_end",
        "extractor",
    ],
)
def test_native_source_identity_hashes_and_extraction_manifest_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    manifest, text = sources["document-version-1"]
    drifted = dict(manifest)
    if field == "document_version_id":
        drifted[field] = "document-version-other"
    elif field == "blob_sha256":
        drifted[field] = hashlib.sha256(b"different blob").hexdigest()
    elif field == "fulltext_sha256":
        drifted[field] = hashlib.sha256(b"different text").hexdigest()
    elif field == "period_end":
        drifted[field] = "2026-04-30"
    else:
        drifted[field] = {"name": "different-extractor"}
    sources["document-version-1"] = drifted, text
    _install_resolver(monkeypatch, sources)

    with pytest.raises(ValueError, match="identity or extraction changed"):
        document_narrative_eval_records(
            golden,
            context,
            source_db_path=source_db,
            source_state_root=state_root,
        )


@pytest.mark.parametrize("damage", ["duplicate_index_id", "missing_gold_case", "duplicate_gold_id"])
def test_native_gold_requires_exact_unique_public_case_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, damage: str
) -> None:
    golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)
    payload = json.loads(context.read_text(encoding="utf-8"))
    index = cast("dict[str, object]", payload["native_gold"])
    if damage == "duplicate_index_id":
        case_ids = cast("list[str]", index["case_ids"])
        case_ids[-1] = case_ids[0]
    else:
        gold_path = (
            state_root / "data/model_evals/transcript_summary_qualification" / str(index["path"])
        )
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        gold_cases = cast("list[dict[str, object]]", gold["cases"])
        if damage == "missing_gold_case":
            gold_cases.pop()
        else:
            gold_cases[-1]["case_id"] = gold_cases[0]["case_id"]
        gold_bytes = json.dumps(gold, ensure_ascii=False, sort_keys=True).encode("utf-8")
        old_hash = str(index["sha256"])
        gold_path.unlink()
        new_hash = hashlib.sha256(gold_bytes).hexdigest()
        new_path = gold_path.with_name(f"{new_hash}.json")
        new_path.write_bytes(gold_bytes)
        index["sha256"] = new_hash
        index["path"] = f"gold/{new_hash}.json"
        context.write_text(json.dumps(payload), encoding="utf-8")
        assert old_hash != new_hash

    if damage == "duplicate_index_id":
        context.write_text(json.dumps(payload), encoding="utf-8")
        pattern = "nine unique cases"
    elif damage == "duplicate_gold_id":
        pattern = "case identity is invalid"
    else:
        pattern = "nine cases"
    with pytest.raises(ValueError, match=pattern):
        document_narrative_eval_records(
            golden,
            context,
            source_db_path=source_db,
            source_state_root=state_root,
        )


@pytest.mark.parametrize(
    ("relative_path", "expected_sha"),
    [
        ("gold/../outside.json", "0" * 64),
        ("/absolute.json", "0" * 64),
        ("C:/drive.json", "0" * 64),
        ("//server/share.json", "0" * 64),
    ],
)
def test_private_gold_reader_rejects_nonlocal_or_traversing_paths(
    tmp_path: Path, relative_path: str, expected_sha: str
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    with pytest.raises(ValueError, match="reference is invalid"):
        read_transcript_audit_proof(state_root, relative_path, expected_sha)


def test_private_gold_reader_rejects_changed_bytes(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    gold_dir = state_root / "data/model_evals/transcript_summary_qualification/gold"
    gold_dir.mkdir(parents=True)
    path = gold_dir / "gold.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="proof changed"):
        read_transcript_audit_proof(state_root, "gold/gold.json", "0" * 64)


def test_native_qualification_limits_require_native_index(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="requires native gold"):
        native_transcript_qualification_limits(
            context,
            source_db_path=tmp_path / "source.db",
            source_state_root=tmp_path / "state",
        )


def test_native_qualification_limits_reject_bad_population(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)
    payload = json.loads(context.read_text(encoding="utf-8"))
    index = cast("dict[str, object]", payload["native_gold"])
    cast("list[object]", index["sources"]).pop()
    context.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="must name three sources"):
        native_transcript_qualification_limits(
            context,
            source_db_path=source_db,
            source_state_root=state_root,
        )


def test_native_qualification_limits_require_full_format_positive_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)

    def shorten_positive(gold: object) -> None:
        cases = cast("dict[str, object]", gold)["cases"]
        for case in cast("list[dict[str, object]]", cases):
            if case["case_id"] in {"native-1-0", "native-2-0", "native-3-0"}:
                case["lines"] = [{"quote": "short positive quote", "supported": True}]

    _rewrite_private_gold(context, state_root, shorten_positive)
    with pytest.raises(ValueError, match="400-900 words"):
        native_transcript_qualification_limits(
            context,
            source_db_path=source_db,
            source_state_root=state_root,
        )


def test_native_qualification_limits_ignore_synthetic_padding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)
    limits = native_transcript_qualification_limits(
        context,
        source_db_path=source_db,
        source_state_root=state_root,
    )
    payload = json.loads(context.read_text(encoding="utf-8"))
    cast("list[object]", payload["cases"]).append(
        {
            "case_id": "synthetic-padding",
            "items": [
                {
                    "n": 1,
                    "kind": "transcript",
                    "label": "synthetic",
                    "text": "x" * 200_000,
                }
            ],
            "lines": [{"quote": "synthetic quote", "supported": True}],
        }
    )
    context.write_text(json.dumps(payload), encoding="utf-8")
    padded_limits = native_transcript_qualification_limits(
        context,
        source_db_path=source_db,
        source_state_root=state_root,
    )
    records, _overlimit = document_narrative_eval_records(
        golden,
        context,
        source_db_path=source_db,
        source_state_root=state_root,
    )

    assert padded_limits == limits
    padding_record = next(record for record in records if record[0] == "synthetic-padding")
    assert len(padding_record[2][0].text) == 200_000


@pytest.mark.parametrize("alias_kind", ["same_path", "symlink", "hardlink", "state_data"])
def test_native_run_rejects_ledger_source_alias_before_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alias_kind: str
) -> None:
    _golden, context, source_db, state_root, sources = _corpus_files(tmp_path)
    _install_resolver(monkeypatch, sources)
    ledger = source_db
    if alias_kind == "symlink":
        ledger = tmp_path / "ledger-symlink.db"
        ledger.symlink_to(source_db)
    elif alias_kind == "hardlink":
        ledger = tmp_path / "ledger-hardlink.db"
        os.link(source_db, ledger)
    elif alias_kind == "state_data":
        ledger = state_root / "data" / "ledger.db"

    import llm_budget

    def forbidden_budget(_purpose: str, *, db_path: Path) -> object:
        raise AssertionError("execution reached the budget check")

    monkeypatch.setattr(llm_budget, "require_explicit_blocking_budget", forbidden_budget)

    with pytest.raises(ValueError, match=r"must be separate|outside source application data"):
        run_document_narrative_eval(
            db_path=ledger,
            golden_path=_golden,
            context_golden_path=context,
            code_root=tmp_path,
            source_db_path=source_db,
            source_state_root=state_root,
        )

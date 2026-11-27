"""Tests for MBI-calibrated deep_dive_analysis Mode-B rubric eval (Item 1)."""

from __future__ import annotations

import json
from pathlib import Path

from evals.corpora import CORPUS_LOADERS, load_deep_dive_analysis_corpus
from evals.rubric_judge import AUDIT_SPECS, load_rubric, run_rubric_eval

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_deep_dive_analysis_rubric_is_valid_and_registered() -> None:
    assert "deep_dive_analysis" in AUDIT_SPECS
    assert "deep_dive_analysis" in CORPUS_LOADERS
    rubric = load_rubric(
        PROJECT_ROOT / AUDIT_SPECS["deep_dive_analysis"].rubric_relpath,
        purpose="deep_dive_analysis",
    )
    assert rubric.purpose == "deep_dive_analysis"
    assert rubric.pass_threshold == 0.70
    assert rubric.facet_ids == (
        "driver_and_unit_discipline",
        "footnote_to_model_linkage",
        "priced_in_reverse_dcf",
        "steelman_and_cross_check",
        "grounding_and_provenance",
    )


def test_load_deep_dive_analysis_corpus_filters_non_report_files(tmp_path: Path) -> None:
    research_dir = tmp_path / "output" / "research"
    (research_dir / "RDDT").mkdir(parents=True)
    (research_dir / "MELI" / "backfill-validation-2026-10-22").mkdir(parents=True)
    (research_dir / "allocation-2026-10-15").mkdir(parents=True)
    (research_dir / "ONON" / "full-memo-2026-10-04").mkdir(parents=True)

    import os

    rddt_body = "# RDDT Deep Dive\n\n" + ("ARPU and data-licensing unit economics. " * 60)
    onon_body = "# ONON Initiation Memo\n\n" + ("DTC mix and wholesale channel margin. " * 60)
    rddt_path = research_dir / "RDDT" / "brief-2026-11-07.md"
    rddt_path.write_text(rddt_body, encoding="utf-8")
    (research_dir / "ONON" / "full-memo-2026-10-04" / "ONON-initiation-memo.md").write_text(
        onon_body, encoding="utf-8"
    )
    # Duplicate copy with older mtime should dedup by content hash in favor of newer RDDT copy.
    dup_path = research_dir / "MELI" / "duplicate-copy.md"
    dup_path.write_text(rddt_body, encoding="utf-8")
    os.utime(dup_path, (1_700_000_000, 1_700_000_000))
    # Must be excluded: backfill, allocation, handoff/readme/short.
    (research_dir / "MELI" / "backfill-validation-2026-10-22" / "summary.md").write_text(
        rddt_body, encoding="utf-8"
    )
    (research_dir / "allocation-2026-10-15" / "plan.md").write_text(rddt_body, encoding="utf-8")
    (research_dir / "ONON" / "full-memo-2026-10-04" / "HANDOFF.md").write_text(
        rddt_body, encoding="utf-8"
    )
    (research_dir / "RDDT" / "short.md").write_text("too short", encoding="utf-8")

    items = load_deep_dive_analysis_corpus(tmp_path)
    ids = {item.item_id for item in items}
    assert ids == {
        "research:RDDT/brief-2026-11-07.md",
        "research:ONON/full-memo-2026-10-04/ONON-initiation-memo.md",
    }
    by_id = {item.item_id: item for item in items}
    assert by_id["research:RDDT/brief-2026-11-07.md"].ticker == "RDDT"
    assert by_id["research:ONON/full-memo-2026-10-04/ONON-initiation-memo.md"].ticker == "ONON"


def test_run_rubric_audit_scores_and_fails_closed_on_missing_facet(tmp_path: Path) -> None:
    research_dir = tmp_path / "output" / "research" / "RDDT"
    research_dir.mkdir(parents=True)
    (research_dir / "brief-2026-11-07.md").write_text(
        "# RDDT Deep Dive\n\n" + ("ARPU and gross margin bridge. " * 60),
        encoding="utf-8",
    )

    valid_verdict = json.dumps(
        {
            "facet_scores": {
                "driver_and_unit_discipline": 1.0,
                "footnote_to_model_linkage": 0.5,
                "priced_in_reverse_dcf": 1.0,
                "steelman_and_cross_check": 0.5,
                "grounding_and_provenance": 1.0,
            },
            "rationale": "Strong unit math; lacks explicit footnote commitments linkage.",
        }
    )
    summary = run_rubric_eval(
        "deep_dive_analysis",
        db_path=tmp_path / "unused.db",
        repo_root=tmp_path,
        code_root=PROJECT_ROOT,
        rubric_path=PROJECT_ROOT / "evals" / "rubrics" / "deep_dive_analysis.md",
        caller=lambda *_a, **_k: valid_verdict,
    )
    assert summary.n_cases == 1
    assert summary.n_pass == 1
    assert summary.avg_score == 0.8

    # Missing a facet key must fail closed with failure_stage='judge' and score=0.0.
    bad_verdict = json.dumps(
        {
            "facet_scores": {"driver_and_unit_discipline": 1.0},
            "rationale": "Incomplete keys.",
        }
    )
    failed = run_rubric_eval(
        "deep_dive_analysis",
        db_path=tmp_path / "unused.db",
        repo_root=tmp_path,
        code_root=PROJECT_ROOT,
        rubric_path=PROJECT_ROOT / "evals" / "rubrics" / "deep_dive_analysis.md",
        caller=lambda *_a, **_k: bad_verdict,
    )
    assert failed.n_cases == 1
    assert failed.n_pass == 0
    assert failed.avg_score == 0.0
    assert failed.cases[0].failure_stage == "judge"

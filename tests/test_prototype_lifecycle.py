"""Structural lifecycle gates for dormant and retired prototypes."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dormant_prototypes_are_implemented_but_not_scheduled() -> None:
    manifest = json.loads((ROOT / "cron" / "task_manifest.json").read_text(encoding="utf-8"))
    serialized = json.dumps(manifest).lower()
    assert "fetch_13f" not in serialized
    assert "sec_delta" not in serialized
    assert (ROOT / "execution" / "fetch_13f.py").is_file()
    assert (ROOT / "src" / "pipeline" / "sec_delta_planner.py").is_file()
    assert (ROOT / "src" / "pipeline" / "sec_delta_admission.py").is_file()


def test_retired_podcast_has_no_executable_or_eval_surface() -> None:
    removed = (
        "execution/fetch_podcast_rss.py",
        "execution/summarize_podcast_episodes.py",
        "src/signals/takeaway.py",
        "src/evals/podcast_takeaway.py",
        "evals/golden/podcast_takeaway_summary.json",
    )
    assert all(not (ROOT / path).exists() for path in removed)
    for path in (
        ROOT / "src" / "llm" / "cli.py",
        ROOT / "src" / "llm" / "prompt_versions.py",
        ROOT / "src" / "evals" / "run_registry.py",
        ROOT / "execution" / "run_llm_evals.py",
        ROOT / "alembic" / "versions" / "0003_restore_baseline_defaults.py",
    ):
        assert "podcast_takeaway_summary" not in path.read_text(encoding="utf-8")

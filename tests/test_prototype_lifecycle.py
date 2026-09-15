"""Structural lifecycle gates for on-demand-only and retired workflows."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_on_demand_disclosure_scans_have_no_scheduler_surface() -> None:
    manifest = json.loads((ROOT / "cron" / "task_manifest.json").read_text(encoding="utf-8"))
    scheduler_text = "\n".join(
        [
            json.dumps(manifest),
            (ROOT / "cron" / "TASKS.generated.md").read_text(encoding="utf-8"),
            (ROOT / "cron" / "register_tasks.generated.ps1").read_text(encoding="utf-8"),
        ]
    ).lower()
    forbidden = (
        "fetch_13f",
        "fetch_congressional_trades",
        "fetch_congress_trades",
        "fetch_house_trades",
        "fetch_senate_trades",
        "politician_trades",
    )
    assert all(name not in scheduler_text for name in forbidden)

    cron_names = "\n".join(path.name.lower() for path in (ROOT / "cron").iterdir())
    assert all(name not in cron_names for name in forbidden)

    retired_13f_surface = (
        "execution/fetch_13f.py",
        "execution/recalibrate_investor_weights.py",
        "execution/resolve_manager_ciks.py",
        "src/discovery/thirteenf.py",
    )
    assert all(not (ROOT / path).exists() for path in retired_13f_surface)


def test_sec_delta_prototype_is_implemented_but_not_scheduled() -> None:
    manifest = json.loads((ROOT / "cron" / "task_manifest.json").read_text(encoding="utf-8"))
    assert "sec_delta" not in json.dumps(manifest).lower()
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

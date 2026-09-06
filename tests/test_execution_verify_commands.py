from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from execution import verify_calendars, verify_evidence_judging, verify_reader_parity


def test_verify_calendars_main_uses_shared_db_default_and_json(
    monkeypatch: Any, capsys: Any
) -> None:
    captured: dict[str, Any] = {}

    def fake_audit(
        db_path: Path, today: object | None = None
    ) -> verify_calendars.CalendarAuditResult:
        captured["db_path"] = db_path
        return verify_calendars.CalendarAuditResult(
            timestamp_utc="2026-09-05T00:00:00Z",
            calendar_pacific_today="2026-09-05",
            tracked_companies_count=1,
            upcoming_expected_count=1,
            past_reported_count=1,
            upcoming_strip_items_count=1,
            integrity_pass=True,
            issues=[],
            sample_upcoming=[{"ticker": "META"}],
        )

    monkeypatch.setattr(verify_calendars, "audit_calendars", fake_audit)

    assert verify_calendars.main(["--json"]) == 0
    assert captured["db_path"] == Path("data/portfolio.db")
    payload = json.loads(str(capsys.readouterr().out))
    assert payload["integrity_pass"] is True
    assert payload["sample_upcoming"][0]["ticker"] == "META"


def test_verify_reader_parity_smoke_uses_shared_bootstrap(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    class _Receipt:
        def __init__(self, status: str) -> None:
            self._status = status

        def model_dump(self, mode: str = "json") -> dict[str, str]:
            return {"status": self._status}

    class _Verifier:
        def __init__(self, repo_root: Path) -> None:
            captured["repo_root"] = repo_root

        def verify_price_parity(self, _ticker: str) -> _Receipt:
            return _Receipt(verify_reader_parity.ParityStatus.VERIFIED_MATCH.value)

        def verify_estimates_parity(self, _ticker: str) -> _Receipt:
            return _Receipt(verify_reader_parity.ParityStatus.VERIFIED_MATCH.value)

        def verify_segments_parity(self, _ticker: str, dim_type: str) -> _Receipt:
            return _Receipt(verify_reader_parity.ParityStatus.VERIFIED_MATCH.value)

        def verify_filing_sections_parity(self, _ticker: str, form: str) -> _Receipt:
            return _Receipt(verify_reader_parity.ParityStatus.VERIFIED_MATCH.value)

    captured: dict[str, Path] = {}
    monkeypatch.setattr(verify_reader_parity, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify_reader_parity, "DualReadShadowingVerifier", _Verifier)

    assert verify_reader_parity.main([]) is None
    payload = json.loads((tmp_path / ".tmp" / "reader_parity_receipt.json").read_text())
    assert captured["repo_root"] == tmp_path
    assert payload["status"] == "PASS"
    assert "verified matches" in capsys.readouterr().out


def test_verify_evidence_judging_smoke_uses_shared_bootstrap(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    class _Receipt:
        def __init__(self, status: verify_evidence_judging.EvidenceJudgeStatus) -> None:
            self.status = status

        def model_dump(self, mode: str = "json") -> dict[str, str]:
            return {"status": self.status.value}

    class _PopAudit:
        is_population_complete = True
        tasks_with_valid_receipts = 8
        total_tasks_in_frame = 8

        def model_dump(self, mode: str = "json") -> dict[str, object]:
            return {"complete": True}

    class _Enforcer:
        def evaluate_task(self, **kwargs: Any) -> _Receipt:
            if kwargs.get("mode") == verify_evidence_judging.JudgeMode.SHADOW:
                return _Receipt(verify_evidence_judging.EvidenceJudgeStatus.PASS)
            if kwargs.get("owner_ratification") is False:
                return _Receipt(verify_evidence_judging.EvidenceJudgeStatus.BLOCK)
            return _Receipt(verify_evidence_judging.EvidenceJudgeStatus.PASS)

    captured: dict[str, object] = {}

    class _Auditor:
        def __init__(self, repo_root: Path) -> None:
            captured["repo_root"] = repo_root

        def audit_frame(self, frame_name: str, expected_task_ids: list[str]) -> _PopAudit:
            captured["frame_name"] = frame_name
            captured["task_ids"] = expected_task_ids
            return _PopAudit()

    monkeypatch.setattr(verify_evidence_judging, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify_evidence_judging, "TaskPopulationFrameAuditor", _Auditor)
    monkeypatch.setattr(verify_evidence_judging, "EvidenceJudgeEnforcer", _Enforcer)

    def fake_sample_size(**_kwargs: object) -> int:
        return 3

    monkeypatch.setattr(verify_evidence_judging, "derive_statistical_sample_size", fake_sample_size)

    assert verify_evidence_judging.main([]) is None
    payload = json.loads((tmp_path / ".tmp" / "evidence_governance_receipt.json").read_text())
    assert captured["repo_root"] == tmp_path
    assert captured["frame_name"] == "wave1_wave2_linear_backlog"
    assert payload["status"] == "PASS"
    assert "Receipt written to:" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("script_name", "expected_flag"),
    [
        ("verify_calendars.py", "--db"),
        ("verify_daily_chain.py", "--db-path"),
        ("verify_evidence_judging.py", "--output-receipt"),
        ("verify_ir_home_authorities.py", "--db"),
        ("verify_reader_parity.py", "--output-receipt"),
        ("verify_scheduler_wrappers.py", "--cron-dir"),
    ],
)
def test_verify_command_scripts_support_direct_help(script_name: str, expected_flag: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(project_root / "execution" / script_name), "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert expected_flag in result.stdout

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows .NET Framework")
def test_launcher_validates_low_labels_by_meaning(tmp_path: Path) -> None:
    """Compile the retained source and exercise its actual label validator."""
    windows = Path(os.environ["SYSTEMROOT"])
    compiler = windows / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not compiler.is_file():
        pytest.skip("Windows .NET Framework compiler is unavailable")
    launcher = tmp_path / "launcher.exe"
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            f"/out:{launcher}",
            str(ROOT / "execution/filing_xbrl_appcontainer_launcher.cs"),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tests/native/filing_xbrl_label_checks.ps1"),
            "-Launcher",
            str(launcher),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    receipt = json.loads(result.stdout)
    assert receipt["passed"] is True
    assert len(receipt["cases"]) == 10
    assert receipt["global_mutex"]["contention_blocked"] is True
    assert receipt["global_mutex"]["acquired_after_release"] is True
    assert receipt["global_mutex"]["effective_security_validated"] is True
    assert receipt["old_oracle_accepts_observed_form"] is False


def test_launcher_owns_suspended_child_before_execution() -> None:
    source = (ROOT / "execution" / "filing_xbrl_appcontainer_launcher.cs").read_text(
        encoding="utf-8"
    )
    create = source.index("if (!CreateProcess(")
    assign = source.index("if (!AssignProcessToJobObject(", create)
    verify = source.index("if (!IsAppContainer(", assign)
    resume = source.index("if (ResumeThread(", verify)

    assert "ExtendedStartupInfoPresent | CreateNoWindow | CreateSuspended" in source
    assert "JobObjectLimitKillOnJobClose" in source
    assert create < assign < verify < resume


def test_launcher_terminates_nonterminal_child_before_closing_job() -> None:
    source = (ROOT / "execution" / "filing_xbrl_appcontainer_launcher.cs").read_text(
        encoding="utf-8"
    )
    cleanup = source.index("if (process.hProcess != IntPtr.Zero && !childTerminal)")
    terminate = source.index("TerminateProcess(process.hProcess, 127);", cleanup)
    wait = source.index("WaitForSingleObject(process.hProcess, Infinite);", terminate)
    close_job = source.index("CloseHandle(job);", wait)

    assert cleanup < terminate < wait < close_job


def test_launcher_reconciles_only_safe_stale_read_acl() -> None:
    source = (ROOT / "execution" / "filing_xbrl_appcontainer_launcher.cs").read_text(
        encoding="utf-8"
    )

    assert "sandbox root has an unsafe pre-existing AppContainer ACL" in source
    assert "security.GetAccessRules(\n            true,\n            true," in source
    assert "if (existing.IsInherited)" in source
    assert "security.RemoveAccessRuleSpecific(existing);" in source
    assert "FileSystemRights.WriteData" in source
    assert "FileSystemRights.ChangePermissions" in source


def test_launcher_serializes_sessions_with_a_protected_operator_mutex() -> None:
    source = (ROOT / "execution/filing_xbrl_appcontainer_launcher.cs").read_text(encoding="utf-8")
    assert '"Global\\\\EarningsSummaryFilingXbrlLauncherV2"' in source
    assert "using (Mutex mutex = OpenInvocationMutex())" in source
    assert "security.SetAccessRuleProtection(true, false);" in source
    assert "WellKnownSidType.LocalSystemSid" in source
    assert "WellKnownSidType.BuiltinAdministratorsSid" in source
    assert "actual.AreAccessRulesProtected" in source
    assert "if (rules.Count != 2)" in source

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

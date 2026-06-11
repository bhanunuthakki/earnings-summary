"""Tests for execution/run_morning_pipeline.py — the morning orchestrator.

The orchestrator chains four subprocess stages (news -> triggers -> feed ->
validate). Its load-bearing contract is resilience: it must attempt every
non-skipped stage even when an earlier one fails or times out, and report the
count of failed stages as the exit code only after all stages have run.

``subprocess.run`` is monkeypatched throughout — no real processes are spawned.
A recording fake captures each invocation's argv (so we can assert call order,
script names, and pass-through args) and returns a per-script-configurable
exit code (so one stage can fail while the others succeed). Tests assert
structural properties — call counts/order, exit codes, summary statuses — never
on log wording or echoed output text.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from execution import run_morning_pipeline

# Script basenames in canonical run order, used to assert dispatch order.
NEWS_SCRIPT = "fetch_news.py"
TRIGGERS_SCRIPT = "run_triggers.py"
FEED_SCRIPT = "build_alert_feed.py"
VALIDATE_SCRIPT = "run_validation_engine.py"


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess.

    Only the attributes the orchestrator reads (returncode, stdout, stderr)
    are populated.
    """

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRun:
    """Records subprocess.run calls; returns a per-script-configurable result.

    ``returncodes`` maps a script basename -> exit code (default 0), so a test
    can make ``run_triggers.py`` fail while the two render stages succeed.
    ``timeout_scripts`` is the set of script basenames for which the call
    raises ``subprocess.TimeoutExpired`` instead of returning — exercising the
    orchestrator's timeout-tolerance path.
    """

    def __init__(
        self,
        *,
        returncodes: dict[str, int] | None = None,
        timeout_scripts: set[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._returncodes = returncodes or {}
        self._timeout_scripts = timeout_scripts or set()

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeCompleted:
        self.calls.append(list(argv))
        script = _script_of(argv)
        if script in self._timeout_scripts:
            timeout = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(
                cmd=argv, timeout=timeout if isinstance(timeout, float | int) else 0
            )
        return _FakeCompleted(returncode=self._returncodes.get(script or "", 0))

    @property
    def scripts(self) -> list[str]:
        """The ordered list of script basenames that were invoked."""
        return [s for c in self.calls if (s := _script_of(c)) is not None]


def _script_of(argv: list[str]) -> str | None:
    """The basename of the first ``.py`` token in an argv (the script path).

    Robust to ``sys.executable`` being an absolute path — we key on the script,
    not argv[0].
    """
    for tok in argv:
        if tok.endswith(".py"):
            return Path(tok).name
    return None


def _parse_summary(out: str) -> dict[str, object]:
    """Extract the final JSON summary the orchestrator prints to stdout.

    The summary is a flat object printed last, so the slice from the final
    ``{`` to the final ``}`` is exactly it. Mocked children emit no stdout, so
    there are no competing JSON blobs.
    """
    start = out.rfind("{")
    end = out.rfind("}")
    assert start != -1 and end != -1 and end > start, f"no summary JSON in:\n{out}"
    parsed = json.loads(out[start : end + 1])
    assert isinstance(parsed, dict)
    return cast("dict[str, object]", parsed)


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _RecordingRun) -> None:
    # String target so the patch is applied to subprocess.run as seen by the
    # orchestrator, without a static `run_morning_pipeline.subprocess` access.
    monkeypatch.setattr("execution.run_morning_pipeline.subprocess.run", fake)


# ---------------------------------------------------------------------------
# Happy path — all four stages succeed
# ---------------------------------------------------------------------------


def test_all_stages_succeed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Four stages return 0 → exit 0, summary all-ok, and the four scripts
    are invoked exactly once each in news -> triggers -> feed -> validate
    order."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])

    assert rc == 0
    assert fake.scripts == [
        NEWS_SCRIPT,
        TRIGGERS_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0_news"] == "ok"
    assert summary["stage_1_triggers"] == "ok"
    assert summary["stage_2_feed"] == "ok"
    assert summary["stage_3_validate"] == "ok"
    assert "elapsed_seconds" in summary


# ---------------------------------------------------------------------------
# Resilience — a failing stage never short-circuits the rest
# ---------------------------------------------------------------------------


def test_stage1_failure_still_runs_feed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If triggers fails, the feed render MUST still run — it is read-only
    over existing alerts, so a trigger failure cannot be allowed to leave the
    user with a stale feed. Exit code reflects exactly one failed stage."""
    fake = _RecordingRun(returncodes={TRIGGERS_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])

    assert rc == 1
    assert fake.scripts == [
        NEWS_SCRIPT,
        TRIGGERS_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_1_triggers"] == "failed"
    assert summary["stage_2_feed"] == "ok"
    assert summary["stage_3_validate"] == "ok"


def test_feed_failure_still_runs_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the feed render fails, the validation gate still runs."""
    fake = _RecordingRun(returncodes={FEED_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])

    assert rc == 1
    assert fake.scripts == [
        NEWS_SCRIPT,
        TRIGGERS_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_1_triggers"] == "ok"
    assert summary["stage_2_feed"] == "failed"
    assert summary["stage_3_validate"] == "ok"


def test_all_stages_fail_exit_code_counts_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every stage failing → all four still attempted, exit code == 4."""
    fake = _RecordingRun(
        returncodes={NEWS_SCRIPT: 1, TRIGGERS_SCRIPT: 1, FEED_SCRIPT: 1, VALIDATE_SCRIPT: 2}
    )
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])

    assert rc == 4
    assert fake.scripts == [
        NEWS_SCRIPT,
        TRIGGERS_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0_news"] == "failed"
    assert summary["stage_1_triggers"] == "failed"
    assert summary["stage_2_feed"] == "failed"
    assert summary["stage_3_validate"] == "failed"


# ---------------------------------------------------------------------------
# Timeout tolerance — a hung stage 1 is caught, the renders still run
# ---------------------------------------------------------------------------


def test_stage1_timeout_is_caught_and_renders_still_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A TimeoutExpired from the triggers subprocess must be caught (not
    propagate), counted as a failed stage, and NOT prevent the feed from
    running."""
    fake = _RecordingRun(timeout_scripts={TRIGGERS_SCRIPT})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])

    assert rc == 1
    assert fake.scripts == [
        NEWS_SCRIPT,
        TRIGGERS_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_1_triggers"] == "failed"
    assert summary["stage_2_feed"] == "ok"
    assert summary["stage_3_validate"] == "ok"


# ---------------------------------------------------------------------------
# --skip-triggers — only the feed render runs
# ---------------------------------------------------------------------------


def test_skip_triggers_runs_only_the_feed_render(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--skip-triggers omits stage 1 entirely: exactly one subprocess call
    (the feed), the trigger script is never invoked, and the summary marks
    stage 1 as skipped (which is not a failure → exit 0)."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main(["--skip-triggers"])

    assert rc == 0
    assert fake.scripts == [FEED_SCRIPT]
    assert TRIGGERS_SCRIPT not in fake.scripts
    # --skip-triggers also skips stage 0 (no point fetching news we won't classify).
    assert NEWS_SCRIPT not in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0_news"] == "skipped"
    assert summary["stage_1_triggers"] == "skipped"
    assert summary["stage_2_feed"] == "ok"
    # --skip-triggers (re-render only) also skips the validation gate.
    assert summary["stage_3_validate"] == "skipped"
    assert VALIDATE_SCRIPT not in fake.scripts


# ---------------------------------------------------------------------------
# Stage 3 — data validation gate (runs last; HALT -> failed stage; skippable)
# ---------------------------------------------------------------------------


def test_validation_gate_runs_last_with_gate_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 3 runs the validation engine AFTER the feed render and passes
    --gate, so egregious data is checked once the day's data is in place."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 0
    assert fake.scripts[-1] == VALIDATE_SCRIPT  # last, after feed

    validate_argv = next(c for c in fake.calls if _script_of(c) == VALIDATE_SCRIPT)
    assert "--gate" in validate_argv
    # The gate takes neither --user-id nor --max-cost-usd.
    assert "--user-id" not in validate_argv
    assert "--max-cost-usd" not in validate_argv

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_3_validate"] == "ok"


def test_validation_halt_counts_as_failed_stage_after_renders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A HALT verdict (the engine exits 2) is a FAILED stage — it raises the
    pipeline exit code for monitoring but the feed already rendered and is not
    affected (the gate is last)."""
    fake = _RecordingRun(returncodes={VALIDATE_SCRIPT: 2})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the validation stage failed
    assert fake.scripts == [
        NEWS_SCRIPT,
        TRIGGERS_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_2_feed"] == "ok"
    assert summary["stage_3_validate"] == "failed"


def test_skip_validation_removes_only_stage3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--skip-validation drops stage 3 but keeps news + triggers + the feed."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main(["--skip-validation"])
    assert rc == 0
    assert fake.scripts == [NEWS_SCRIPT, TRIGGERS_SCRIPT, FEED_SCRIPT]
    assert VALIDATE_SCRIPT not in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_3_validate"] == "skipped"


def test_validation_gate_receives_db_path_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--db-path is forwarded to the validation gate too (the engine accepts it as
    an alias for --db), so an alternate DB flows through the whole pipeline."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)
    db_path = tmp_path / "alt.db"

    rc = run_morning_pipeline.main(["--db-path", str(db_path)])
    assert rc == 0

    validate_argv = next(c for c in fake.calls if _script_of(c) == VALIDATE_SCRIPT)
    assert _has_flag(validate_argv, "--db-path", str(db_path))


# ---------------------------------------------------------------------------
# Argument pass-through
# ---------------------------------------------------------------------------


def test_args_passed_through_to_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """--max-cost-usd + --user-id reach the trigger stage; --user-id reaches
    the feed stage too; --max-cost-usd does NOT leak to the renderer (it has
    no such flag)."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main(["--max-cost-usd", "5", "--user-id", "alice"])
    assert rc == 0

    by_script = {_script_of(c): c for c in fake.calls}
    triggers_argv = by_script[TRIGGERS_SCRIPT]
    feed_argv = by_script[FEED_SCRIPT]
    news_argv = by_script[NEWS_SCRIPT]

    # The news fetcher takes neither --user-id nor --max-cost-usd.
    assert "--user-id" not in news_argv
    assert "--max-cost-usd" not in news_argv

    # The contract is the forwarded numeric value, not its string form —
    # argparse (type=float) normalizes "5" to "5.0", which run_triggers parses
    # back to the same float. Assert the value, not the formatting.
    cost = _flag_value(triggers_argv, "--max-cost-usd")
    assert cost is not None and float(cost) == 5.0
    assert _has_flag(triggers_argv, "--user-id", "alice")

    assert _has_flag(feed_argv, "--user-id", "alice")
    assert "--max-cost-usd" not in feed_argv


def test_db_path_passed_to_all_stages_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--db-path is forwarded to every stage so a test/alternate DB flows
    through the whole pipeline."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)
    db_path = tmp_path / "alt.db"

    rc = run_morning_pipeline.main(["--db-path", str(db_path)])
    assert rc == 0

    for argv in fake.calls:
        assert _has_flag(argv, "--db-path", str(db_path))


def test_db_path_omitted_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When --db-path is not passed, no stage receives a --db-path flag — each
    script falls back to its own default DB resolution rather than a literal
    'None'."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 0

    for argv in fake.calls:
        assert "--db-path" not in argv


# ---------------------------------------------------------------------------
# Stage 0 — news fetch (before triggers; skip + source-forwarding + resilience)
# ---------------------------------------------------------------------------


def test_stage0_news_runs_first_with_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default Stage 0 runs first and invokes fetch_news.py --source auto."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 0
    assert fake.scripts[0] == NEWS_SCRIPT  # ordered before triggers

    news_argv = next(c for c in fake.calls if _script_of(c) == NEWS_SCRIPT)
    assert _flag_value(news_argv, "--source") == "auto"


def test_skip_news_removes_only_stage0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--skip-news drops stage 0 but keeps triggers + the two renders."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main(["--skip-news"])
    assert rc == 0
    assert fake.scripts == [TRIGGERS_SCRIPT, FEED_SCRIPT, VALIDATE_SCRIPT]
    assert NEWS_SCRIPT not in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0_news"] == "skipped"
    assert summary["stage_1_triggers"] == "ok"
    # --skip-news keeps the data-validation gate (stage 3).
    assert summary["stage_3_validate"] == "ok"


def test_news_source_forwarded_to_stage0(monkeypatch: pytest.MonkeyPatch) -> None:
    """--news-source flows through to fetch_news.py's --source."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main(["--news-source", "websearch"])
    assert rc == 0

    news_argv = next(c for c in fake.calls if _script_of(c) == NEWS_SCRIPT)
    assert _flag_value(news_argv, "--source") == "websearch"


def test_news_failure_does_not_stop_triggers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed stage 0 is counted but never blocks triggers/feed — the
    trigger sweep runs over whatever news already exists, degrading to none."""
    fake = _RecordingRun(returncodes={NEWS_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the news stage failed
    assert fake.scripts == [
        NEWS_SCRIPT,
        TRIGGERS_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0_news"] == "failed"
    assert summary["stage_1_triggers"] == "ok"
    assert summary["stage_2_feed"] == "ok"


def _has_flag(argv: list[str], flag: str, value: str) -> bool:
    """True iff ``argv`` contains ``flag`` immediately followed by ``value``."""
    return any(tok == flag and argv[i + 1] == value for i, tok in enumerate(argv[:-1]))


def _flag_value(argv: list[str], flag: str) -> str | None:
    """The token immediately following ``flag`` in ``argv``, or None if absent."""
    for i, tok in enumerate(argv[:-1]):
        if tok == flag:
            return argv[i + 1]
    return None

"""Tests for execution/run_morning_pipeline.py — the morning orchestrator.

The orchestrator runs an unconditional environment preflight, then chains
the subprocess stages (news -> decisions -> lifecycle -> fundamentals ->
reprice -> candidate_fit -> factor_proxies -> triggers -> standup -> feed ->
validate). Its load-bearing contract is
resilience: it must attempt every non-skipped stage even when an earlier one
fails or times out, and report the
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
PREFLIGHT_SCRIPT = "validate_environment.py"
NEWS_SCRIPT = "fetch_news.py"
LIST_TYPE_SCRIPT = "sync_list_type_from_holdings.py"
DECISIONS_SCRIPT = "record_decisions.py"
LIFECYCLE_SCRIPT = "sync_position_lifecycle.py"
DECISION_ACTIONS_SCRIPT = "reconcile_decision_actions.py"
FUNDAMENTALS_SCRIPT = "refresh_cockpit_fundamentals.py"
DERIVED_METRICS_SCRIPT = "compute_derived_metrics.py"
REPRICE_SCRIPT = "reprice_dcf.py"
CANDIDATE_FIT_SCRIPT = "refresh_candidate_fit.py"
FACTOR_PROXIES_SCRIPT = "fetch_factor_proxies.py"
POSITION_GUARD_SCRIPT = "refresh_position_guard.py"
TRIGGERS_SCRIPT = "run_triggers.py"
STANDUP_SCRIPT = "run_standup.py"
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
    """Every stage returns 0 → exit 0, summary all-ok, and the scripts are
    invoked exactly once each in preflight -> news -> decisions -> lifecycle ->
    fundamentals -> triggers -> feed -> validate order."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])

    assert rc == 0
    assert fake.scripts == [
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_preflight"] == "ok"
    assert summary["stage_0_news"] == "ok"
    assert summary["stage_0a_list_type"] == "ok"
    assert summary["stage_0b_decisions"] == "ok"
    assert summary["stage_0c_lifecycle"] == "ok"
    assert summary["stage_0c2_decision_actions"] == "ok"
    assert summary["stage_0d_fundamentals"] == "ok"
    assert summary["stage_0d2_derived_metrics"] == "ok"
    assert summary["stage_0e_reprice"] == "ok"
    assert summary["stage_0f_candidate_fit"] == "ok"
    assert summary["stage_0g_factor_proxies"] == "ok"
    assert summary["stage_0h_position_guard"] == "ok"
    assert summary["stage_1_triggers"] == "ok"
    assert summary["stage_1b_standup"] == "ok"
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
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
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
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
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
    """Every stage failing (preflight included) → all sixteen still attempted,
    exit code == 16."""
    fake = _RecordingRun(
        returncodes={
            PREFLIGHT_SCRIPT: 1,
            NEWS_SCRIPT: 1,
            LIST_TYPE_SCRIPT: 1,
            DECISIONS_SCRIPT: 1,
            LIFECYCLE_SCRIPT: 1,
            DECISION_ACTIONS_SCRIPT: 1,
            FUNDAMENTALS_SCRIPT: 1,
            DERIVED_METRICS_SCRIPT: 1,
            REPRICE_SCRIPT: 1,
            CANDIDATE_FIT_SCRIPT: 1,
            FACTOR_PROXIES_SCRIPT: 1,
            POSITION_GUARD_SCRIPT: 1,
            TRIGGERS_SCRIPT: 1,
            STANDUP_SCRIPT: 1,
            FEED_SCRIPT: 1,
            VALIDATE_SCRIPT: 2,
        }
    )
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])

    assert rc == 16
    assert fake.scripts == [
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_preflight"] == "failed"
    assert summary["stage_0_news"] == "failed"
    assert summary["stage_0a_list_type"] == "failed"
    assert summary["stage_0b_decisions"] == "failed"
    assert summary["stage_0c_lifecycle"] == "failed"
    assert summary["stage_0c2_decision_actions"] == "failed"
    assert summary["stage_0d_fundamentals"] == "failed"
    assert summary["stage_0d2_derived_metrics"] == "failed"
    assert summary["stage_0e_reprice"] == "failed"
    assert summary["stage_0f_candidate_fit"] == "failed"
    assert summary["stage_0g_factor_proxies"] == "failed"
    assert summary["stage_0h_position_guard"] == "failed"
    assert summary["stage_1_triggers"] == "failed"
    assert summary["stage_1b_standup"] == "failed"
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
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_1_triggers"] == "failed"
    assert summary["stage_2_feed"] == "ok"
    assert summary["stage_3_validate"] == "ok"


def test_timeout_echoes_partial_output_captured_before_the_kill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A killed-on-timeout child's ``TimeoutExpired`` carries whatever stdout/
    stderr Python drained from the pipes before raising (verified in this
    Python's stdlib: ``subprocess.run(..., timeout=...)`` populates
    ``TimeoutExpired.stdout``/``.stderr`` even though it never returns a
    CompletedProcess). Before the fix, `_run_stage` only echoed output on a
    NORMAL return, so a killed stage's partial progress lines (e.g. a
    per-ticker JSON progress event) were silently discarded and the cron log
    showed a completely empty stage section -- exactly what happened in
    production on the stage_0_news / stage_0e_reprice timeouts. This pins that
    the partial output IS echoed on the timeout path too."""

    def _raise_with_partial_output(argv: list[str], **kwargs: object) -> _FakeCompleted:
        if _script_of(argv) == TRIGGERS_SCRIPT:
            timeout = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(
                cmd=argv,
                timeout=timeout if isinstance(timeout, float | int) else 0,
                output='{"event": "trigger_ticker_done", "ticker": "NU", "i": 3, "n": 98}\n',
                stderr="partial stderr before the kill\n",
            )
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr("execution.run_morning_pipeline.subprocess.run", _raise_with_partial_output)

    rc = run_morning_pipeline.main([])

    assert rc == 1
    captured = capsys.readouterr()
    assert '"ticker": "NU"' in captured.out
    assert "partial stderr before the kill" in captured.err
    assert "timed out after" in captured.err  # the failure banner still fires


# ---------------------------------------------------------------------------
# --skip-triggers — only the feed render runs
# ---------------------------------------------------------------------------


def test_skip_triggers_runs_only_the_feed_render(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--skip-triggers omits stage 1 entirely: only the (unconditional)
    preflight and the feed render run, the trigger script is never invoked,
    and the summary marks stage 1 as skipped (not a failure → exit 0)."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main(["--skip-triggers"])

    assert rc == 0
    assert fake.scripts == [PREFLIGHT_SCRIPT, FEED_SCRIPT]
    assert TRIGGERS_SCRIPT not in fake.scripts
    # --skip-triggers also skips stage 0 (no point fetching news we won't classify)
    # and stages 0b/0c (the decision_condition sensor won't run; the ledger
    # reconciles tomorrow), and stage 1b standup (it watches what the sweep refreshes).
    assert NEWS_SCRIPT not in fake.scripts
    assert DECISIONS_SCRIPT not in fake.scripts
    assert LIST_TYPE_SCRIPT not in fake.scripts
    assert LIFECYCLE_SCRIPT not in fake.scripts
    assert DECISION_ACTIONS_SCRIPT not in fake.scripts
    assert FUNDAMENTALS_SCRIPT not in fake.scripts
    assert DERIVED_METRICS_SCRIPT not in fake.scripts
    assert REPRICE_SCRIPT not in fake.scripts
    assert STANDUP_SCRIPT not in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0_news"] == "skipped"
    assert summary["stage_0a_list_type"] == "skipped"
    assert summary["stage_0b_decisions"] == "skipped"
    assert summary["stage_0c_lifecycle"] == "skipped"
    assert summary["stage_0c2_decision_actions"] == "skipped"
    assert summary["stage_0d_fundamentals"] == "skipped"
    assert summary["stage_0d2_derived_metrics"] == "skipped"
    assert summary["stage_0e_reprice"] == "skipped"
    assert summary["stage_1_triggers"] == "skipped"
    assert summary["stage_1b_standup"] == "skipped"
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
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
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
    assert fake.scripts == [
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
        FEED_SCRIPT,
    ]
    assert VALIDATE_SCRIPT not in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_3_validate"] == "skipped"


def test_skip_standup_removes_only_stage1b(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--skip-standup drops stage 1b (the paid advisory leg) but keeps the
    trigger sweep, the feed render, and the validation gate."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main(["--skip-standup"])
    assert rc == 0
    assert STANDUP_SCRIPT not in fake.scripts
    assert TRIGGERS_SCRIPT in fake.scripts
    assert FEED_SCRIPT in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_1_triggers"] == "ok"
    assert summary["stage_1b_standup"] == "skipped"
    assert summary["stage_2_feed"] == "ok"


def test_stage1b_standup_runs_after_triggers_with_user_and_db_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stage 1b runs run_standup.py AFTER the trigger sweep (so it watches fresh
    decision-condition state) and is forwarded --user-id + --db-path, never
    --max-cost-usd (it owns its own rate-limit / eval-bar defaults)."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)
    db_path = tmp_path / "alt.db"

    rc = run_morning_pipeline.main(["--db-path", str(db_path), "--user-id", "alice"])
    assert rc == 0
    assert fake.scripts.index(TRIGGERS_SCRIPT) < fake.scripts.index(STANDUP_SCRIPT)
    assert fake.scripts.index(STANDUP_SCRIPT) < fake.scripts.index(FEED_SCRIPT)

    standup_argv = next(c for c in fake.calls if _script_of(c) == STANDUP_SCRIPT)
    assert _has_flag(standup_argv, "--user-id", "alice")
    assert _has_flag(standup_argv, "--db-path", str(db_path))
    assert "--max-cost-usd" not in standup_argv


def test_standup_failure_still_runs_feed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A standup failure is counted but never blocks the feed render — it is a
    paid advisory side-channel, not on the feed's critical path."""
    fake = _RecordingRun(returncodes={STANDUP_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1
    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_1b_standup"] == "failed"
    assert summary["stage_2_feed"] == "ok"
    assert summary["stage_3_validate"] == "ok"


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

    # The decisions recorder likewise takes neither (single-operator ledger).
    decisions_argv = by_script[DECISIONS_SCRIPT]
    assert "--user-id" not in decisions_argv
    assert "--max-cost-usd" not in decisions_argv

    # The lifecycle reconciler takes --user-id (its rows are user-scoped) but
    # never --max-cost-usd (no LLM anywhere in it).
    lifecycle_argv = by_script[LIFECYCLE_SCRIPT]
    assert _has_flag(lifecycle_argv, "--user-id", "alice")
    assert "--max-cost-usd" not in lifecycle_argv

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
        if _script_of(argv) == PREFLIGHT_SCRIPT:
            continue  # the env preflight takes no --db-path
        if _script_of(argv) == FACTOR_PROXIES_SCRIPT:
            continue  # no DB at all — takes --repo-root derived from the db
            # override instead (asserted in its own stage-0g test)
        if _script_of(argv) == DERIVED_METRICS_SCRIPT:
            # Its own flag name is --db (asserted in its stage-0d2 test).
            assert _has_flag(argv, "--db", str(db_path))
            continue
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
    """Stage 0 is the first MAIN stage (right after the unconditional
    preflight) and invokes fetch_news.py --source auto."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 0
    assert fake.scripts[0] == PREFLIGHT_SCRIPT  # unconditional, always first
    assert fake.scripts[1] == NEWS_SCRIPT  # ordered before triggers

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
    assert fake.scripts == [
        PREFLIGHT_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]
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
        PREFLIGHT_SCRIPT,
        NEWS_SCRIPT,
        LIST_TYPE_SCRIPT,
        DECISIONS_SCRIPT,
        LIFECYCLE_SCRIPT,
        DECISION_ACTIONS_SCRIPT,
        FUNDAMENTALS_SCRIPT,
        DERIVED_METRICS_SCRIPT,
        REPRICE_SCRIPT,
        CANDIDATE_FIT_SCRIPT,
        FACTOR_PROXIES_SCRIPT,
        POSITION_GUARD_SCRIPT,
        TRIGGERS_SCRIPT,
        STANDUP_SCRIPT,
        FEED_SCRIPT,
        VALIDATE_SCRIPT,
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0_news"] == "failed"
    assert summary["stage_1_triggers"] == "ok"
    assert summary["stage_2_feed"] == "ok"


# ---------------------------------------------------------------------------
# Stage 0b — decision conditions (after news, before triggers)
# ---------------------------------------------------------------------------


def test_stage0b_decisions_runs_between_news_and_triggers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 0b runs record_decisions.py after the news fetch and before the
    trigger sweep, so the decision_condition sensor evaluates conditions
    extracted in the SAME run. A stage-0b failure must not block triggers."""
    fake = _RecordingRun(returncodes={DECISIONS_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the decisions stage failed
    assert fake.scripts.index(NEWS_SCRIPT) < fake.scripts.index(DECISIONS_SCRIPT)
    assert fake.scripts.index(DECISIONS_SCRIPT) < fake.scripts.index(TRIGGERS_SCRIPT)

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0b_decisions"] == "failed"
    assert summary["stage_1_triggers"] == "ok"


def test_stage0c_lifecycle_runs_between_decisions_and_triggers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 0c reconciles the position ledger AFTER stage 0b (so a row opened
    today snapshots the conditions extracted in the same run) and before the
    trigger sweep. Its failure must not block triggers."""
    fake = _RecordingRun(returncodes={LIFECYCLE_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the lifecycle stage failed
    assert fake.scripts.index(DECISIONS_SCRIPT) < fake.scripts.index(LIFECYCLE_SCRIPT)
    assert fake.scripts.index(LIFECYCLE_SCRIPT) < fake.scripts.index(TRIGGERS_SCRIPT)

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0c_lifecycle"] == "failed"
    assert summary["stage_1_triggers"] == "ok"


# ---------------------------------------------------------------------------
# Stage 0e — DCF re-price (after fundamentals, before triggers)
# ---------------------------------------------------------------------------


def test_stage0e_reprice_runs_between_fundamentals_and_triggers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 0e re-prices the DCF over/under AFTER stage 0d (so the fresh price
    leg is in place) and before the trigger sweep, which reads the trim/sell
    ladder. Its failure must not block triggers — over/under degrades to the
    last persisted price rather than blanking."""
    fake = _RecordingRun(returncodes={REPRICE_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the reprice stage failed
    assert fake.scripts.index(FUNDAMENTALS_SCRIPT) < fake.scripts.index(REPRICE_SCRIPT)
    assert fake.scripts.index(REPRICE_SCRIPT) < fake.scripts.index(TRIGGERS_SCRIPT)

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0e_reprice"] == "failed"
    assert summary["stage_1_triggers"] == "ok"


def test_stage0e_reprice_takes_db_path_but_not_user_or_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The re-price is not user-scoped and runs no LLM: only --db-path flows
    through (when set), never --user-id / --max-cost-usd."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)
    db_path = tmp_path / "alt.db"

    rc = run_morning_pipeline.main(["--db-path", str(db_path), "--user-id", "alice"])
    assert rc == 0

    reprice_argv = next(c for c in fake.calls if _script_of(c) == REPRICE_SCRIPT)
    assert _has_flag(reprice_argv, "--db-path", str(db_path))
    assert "--user-id" not in reprice_argv
    assert "--max-cost-usd" not in reprice_argv


def test_stage0d2_derived_metrics_runs_between_fundamentals_and_triggers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 0d2 computes the bottoms-up derived metrics into kpi_facts AFTER
    the fundamentals cache (0d) and BEFORE the trigger sweep (stage 1), which
    reads kpi_facts — the docs/design/bottoms_up_metrics_engine.md §5
    placement. Its failure must not block triggers: existing kpi_facts rows
    simply stay at their last computed values."""
    fake = _RecordingRun(returncodes={DERIVED_METRICS_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the derived-metrics stage failed
    assert fake.scripts.index(FUNDAMENTALS_SCRIPT) < fake.scripts.index(DERIVED_METRICS_SCRIPT)
    assert fake.scripts.index(DERIVED_METRICS_SCRIPT) < fake.scripts.index(TRIGGERS_SCRIPT)

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0d2_derived_metrics"] == "failed"
    assert summary["stage_1_triggers"] == "ok"


def test_stage0d2_derived_metrics_takes_db_flag_and_all_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The derived-metrics CLI is not user-scoped and runs no LLM: --all is
    always passed; the DB override flows through as --db (that CLI's own flag
    name, unlike the other stages' --db-path); never --user-id /
    --max-cost-usd."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)
    db_path = tmp_path / "alt.db"

    rc = run_morning_pipeline.main(["--db-path", str(db_path), "--user-id", "alice"])
    assert rc == 0

    argv = next(c for c in fake.calls if _script_of(c) == DERIVED_METRICS_SCRIPT)
    assert "--all" in argv
    assert _has_flag(argv, "--db", str(db_path))
    assert "--db-path" not in argv
    assert "--user-id" not in argv
    assert "--max-cost-usd" not in argv


def test_stage0g_factor_proxies_runs_between_candidate_fit_and_triggers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 0g refreshes the ETF style-proxy series after the derived-cache
    stages and before triggers; a yfinance outage (stage failure) must not
    block the sweep — the Risk panel degrades to the last-good files."""
    fake = _RecordingRun(returncodes={FACTOR_PROXIES_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the proxies stage failed
    assert fake.scripts.index(CANDIDATE_FIT_SCRIPT) < fake.scripts.index(FACTOR_PROXIES_SCRIPT)
    assert fake.scripts.index(FACTOR_PROXIES_SCRIPT) < fake.scripts.index(TRIGGERS_SCRIPT)

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0g_factor_proxies"] == "failed"
    assert summary["stage_1_triggers"] == "ok"


def test_stage0g_factor_proxies_takes_repo_root_from_db_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The proxy fetch writes data/factor_proxies/ under the db override's repo
    root (never the real repo on a --db-path run); it is not user-scoped, runs
    no LLM, and needs no DB at all. Without the override, no --repo-root is
    passed (the script defaults to its own repo)."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)
    db_path = tmp_path / "data" / "alt.db"

    rc = run_morning_pipeline.main(["--db-path", str(db_path), "--user-id", "alice"])
    assert rc == 0

    proxies_argv = next(c for c in fake.calls if _script_of(c) == FACTOR_PROXIES_SCRIPT)
    assert _has_flag(proxies_argv, "--repo-root", str(tmp_path))
    assert "--db-path" not in proxies_argv
    assert "--user-id" not in proxies_argv
    assert "--max-cost-usd" not in proxies_argv

    fake_default = _RecordingRun()
    _install_fake(monkeypatch, fake_default)
    assert run_morning_pipeline.main([]) == 0
    default_argv = next(c for c in fake_default.calls if _script_of(c) == FACTOR_PROXIES_SCRIPT)
    assert "--repo-root" not in default_argv


def test_stage0h_position_guard_runs_between_factor_proxies_and_triggers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage 0h materializes the naked-position gate cache after the factor
    proxies refresh and before triggers; a failure must not block the sweep —
    the Risk panel degrades to the last-good cache file."""
    fake = _RecordingRun(returncodes={POSITION_GUARD_SCRIPT: 1})
    _install_fake(monkeypatch, fake)

    rc = run_morning_pipeline.main([])
    assert rc == 1  # exactly the position-guard stage failed
    assert fake.scripts.index(FACTOR_PROXIES_SCRIPT) < fake.scripts.index(POSITION_GUARD_SCRIPT)
    assert fake.scripts.index(POSITION_GUARD_SCRIPT) < fake.scripts.index(TRIGGERS_SCRIPT)

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["stage_0h_position_guard"] == "failed"
    assert summary["stage_1_triggers"] == "ok"


def test_stage0h_position_guard_takes_db_path_but_not_user_or_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate is not user-scoped and runs no LLM: only --db-path flows
    through (when set), never --user-id / --max-cost-usd."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)
    db_path = tmp_path / "alt.db"

    rc = run_morning_pipeline.main(["--db-path", str(db_path), "--user-id", "alice"])
    assert rc == 0

    guard_argv = next(c for c in fake.calls if _script_of(c) == POSITION_GUARD_SCRIPT)
    assert _has_flag(guard_argv, "--db-path", str(db_path))
    assert "--user-id" not in guard_argv
    assert "--max-cost-usd" not in guard_argv


def _has_flag(argv: list[str], flag: str, value: str) -> bool:
    """True iff ``argv`` contains ``flag`` immediately followed by ``value``."""
    return any(tok == flag and argv[i + 1] == value for i, tok in enumerate(argv[:-1]))


def _flag_value(argv: list[str], flag: str) -> str | None:
    """The token immediately following ``flag`` in ``argv``, or None if absent."""
    for i, tok in enumerate(argv[:-1]):
        if tok == flag:
            return argv[i + 1]
    return None

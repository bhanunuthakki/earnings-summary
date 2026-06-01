# pyright: reportPrivateUsage=false
#
# Several tests poke module-private helpers (``_build_themes``,
# ``_apply_llm_topics``, ``_load_packages``, ``_verify_setup_once``,
# ``compute_valuation.extract_for_ticker``) to drive each section's LLM-call
# except-branch in isolation. The module-level directive silences only the
# cross-module private-usage rule, preserving every other strict check — this
# matches the repo convention (see src/llm/cli.py's header for the same).
"""A hard LLM CALL exception (not just an empty/unparseable response) must
degrade the affected brief section gracefully instead of aborting the whole
multi-section build — EXCEPT for genuine hard stops, which must propagate.

Companion to ``test_bear_case_resilience.py`` (which pins the empty/unparseable
*response* path). Here the LLM generator / call itself RAISES:

  * Transient operational failures — a subprocess timeout, a non-zero exit, or
    both Claude + Gemini momentarily unavailable (surfaces as a plain
    ``RuntimeError`` out of the fallback chain) — degrade the section to a loud
    MISSING_DATA banner (or drop just the LLM-derived sub-part where the rest of
    the section is deterministic). One flaky call can't nuke every other section
    + the render.
  * Genuine hard stops — ``LLMBudgetExceeded`` (a hard monthly cap) and
    ``LLMSetupError`` (the CLI isn't installed) — PROPAGATE so the build fails
    loudly: re-running won't help and degrading would silently mask an
    over-budget / unconfigured run.

The policy lives in one place (``llm_client.is_hard_stop``); these tests pin
both the classifier and its wiring into all six LLM-driven sections.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_client import LLMBudgetExceeded, LLMSetupError, is_hard_stop
from report.models import (
    BearCaseSection,
    EarningsSection,
    FinancialsSection,
    QAEntry,
    RecentDevelopmentsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
    TranscriptEntry,
)
from report.sections import (
    bear_case,
    earnings,
    exec_compensation,
    qa_roster,
    recent_developments,
    valuation,
)


def _raiser(exc: BaseException) -> Callable[..., object]:
    """Return a stub that raises ``exc`` regardless of how it's called — stands
    in for an LLM generator whose underlying ``call_llm`` blew up."""

    def _stub(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise exc

    return _stub


def _returner(value: object) -> Callable[..., object]:
    """Return a stub that ignores its args and returns ``value`` — a typed
    stand-in for ``lambda`` so the strict type-checker stays happy."""

    def _stub(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return value

    return _stub


# A representative transient operational failure: this is exactly what the
# Gemini-fallback chain raises when the Claude CLI errored AND the fallback is
# unavailable (see src/llm/fallback.py). Plain RuntimeError -> NOT a hard stop.
_TRANSIENT = RuntimeError(
    "Both LLMs failed: Claude CLI errored AND Gemini fallback returned empty response."
)


# ---------------------------------------------------------------------------
# The classifier — single source of truth for the propagate-vs-degrade policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        _TRANSIENT,
        RuntimeError("Claude CLI failed AND no Gemini fallback configured."),
        TimeoutError("call timed out"),
        subprocess.TimeoutExpired("claude", 1200),
        subprocess.CalledProcessError(1, "claude"),
        ValueError("malformed JSON envelope"),
        OSError("binary vanished mid-run"),
        Exception("generic transient"),
    ],
)
def test_is_hard_stop_false_for_transient(exc: BaseException) -> None:
    # A bare RuntimeError (the fallback-chain failure) must NOT be a hard stop,
    # even though LLMBudgetExceeded / LLMSetupError subclass RuntimeError.
    assert is_hard_stop(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        LLMBudgetExceeded("bear_case: monthly cap exceeded"),
        LLMSetupError("Claude Code CLI ('claude') not found in PATH."),
    ],
)
def test_is_hard_stop_true_for_hard_stops(exc: BaseException) -> None:
    assert is_hard_stop(exc) is True


def test_setup_error_is_runtimeerror_subclass() -> None:
    # Back-compat: pre-existing ``except RuntimeError`` / pytest.raises(RuntimeError)
    # call sites that caught the old bare RuntimeError keep working.
    assert issubclass(LLMSetupError, RuntimeError)


def test_verify_setup_once_raises_typed_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_client

    # Force a cold verify + a missing binary.
    monkeypatch.setattr(llm_client, "_setup_verified", False)
    monkeypatch.setattr(llm_client, "_claude_cli_path", None)
    monkeypatch.setattr("llm.cli.shutil.which", _returner(None))
    with pytest.raises(LLMSetupError):
        llm_client._verify_setup_once()


# ---------------------------------------------------------------------------
# §7 Bear case — mirrors test_bear_case_resilience.py, but the CALL raises
# ---------------------------------------------------------------------------


def _thesis() -> ThesisSection:
    return ThesisSection(
        status=SectionStatus.OK,
        thesis_full="NU is LatAm's leading digital bank; the bet is durable Brazil ROE.",
        break_conditions=["ROE sustains below 25% for two quarters"],
    )


def _build_bear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException
) -> BearCaseSection:
    """Run bear_case.build with the LLM generator stubbed to RAISE ``exc``.

    ``force_refresh`` skips the cache so the stub always runs; ``tmp_path`` has
    no portfolio.db so the prompt-assembly helpers degrade to empty strings.
    """
    monkeypatch.setattr("report.sections.bear_case.generate_bear_case", _raiser(exc))
    return bear_case.build(
        ticker="NU",
        repo_root=tmp_path,
        enable_llm=True,
        thesis=_thesis(),
        financials=FinancialsSection(status=SectionStatus.MISSING_DATA),
        segments=SegmentsSection(status=SectionStatus.MISSING_DATA),
        earnings=EarningsSection(status=SectionStatus.MISSING_DATA),
        force_refresh=True,
    )


def test_bear_case_degrades_on_transient_call_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _build_bear(monkeypatch, tmp_path, _TRANSIENT)
    # Loud MISSING_DATA banner — the whole brief still renders.
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None
    assert section.failure_modes == []


def test_bear_case_propagates_budget_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(LLMBudgetExceeded):
        _build_bear(monkeypatch, tmp_path, LLMBudgetExceeded("bear_case: monthly cap exceeded"))


def test_bear_case_propagates_setup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LLMSetupError):
        _build_bear(monkeypatch, tmp_path, LLMSetupError("claude not found in PATH"))


# ---------------------------------------------------------------------------
# §8 Recent developments — unguarded call previously aborted the whole build
# ---------------------------------------------------------------------------


def _build_recent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException
) -> RecentDevelopmentsSection:
    monkeypatch.setattr(
        "report.sections.recent_developments.generate_recent_developments", _raiser(exc)
    )
    return recent_developments.build(
        ticker="NU", repo_root=tmp_path, enable_llm=True, force_refresh=True
    )


def test_recent_developments_degrades_on_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _build_recent(monkeypatch, tmp_path, _TRANSIENT)
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None


def test_recent_developments_propagates_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(LLMBudgetExceeded):
        _build_recent(monkeypatch, tmp_path, LLMBudgetExceeded("recent_developments cap"))


def test_recent_developments_does_not_cache_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A degraded run must NOT write the cache, so a re-run retries the call
    # rather than serving the stub for the whole TTL.
    _build_recent(monkeypatch, tmp_path, _TRANSIENT)
    assert not (tmp_path / recent_developments.NEWS_CACHE_DIRNAME / "NU.json").exists()


# ---------------------------------------------------------------------------
# Q&A roster — previously a bare ``except Exception`` swallowed budget caps
# ---------------------------------------------------------------------------


def _qa_entries() -> list[QAEntry]:
    return [QAEntry(analysts="Jane Doe (BofA)", topic="NIM trajectory", tag="Q&A", question="NIM?")]


def _transcript() -> TranscriptEntry:
    return TranscriptEntry(quarter="Q1", year=2025, source_path="x.txt", text="...")


def test_qa_roster_keeps_regex_labels_on_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("report.sections.qa_roster.generate_qa_topics", _raiser(_TRANSIENT))
    entries = _qa_entries()
    out = qa_roster._apply_llm_topics("NU", tmp_path, _transcript(), entries)
    assert out == entries  # soft-fail: original regex-derived labels preserved


def test_qa_roster_propagates_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "report.sections.qa_roster.generate_qa_topics", _raiser(LLMBudgetExceeded("qa cap"))
    )
    with pytest.raises(LLMBudgetExceeded):
        qa_roster._apply_llm_topics("NU", tmp_path, _transcript(), _qa_entries())


# ---------------------------------------------------------------------------
# §5 Earnings theme split — previously swallowed budget caps too
# ---------------------------------------------------------------------------


def _build_themes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException):
    # Bypass disk reads so a single dummy transcript reaches the LLM call.
    monkeypatch.setattr(
        "report.sections.earnings._read_transcript_text", _returner("prepared remarks text")
    )
    monkeypatch.setattr("llm_client.extract_qa_vs_prepared_themes", _raiser(exc))
    return earnings._build_themes(
        ticker="NU",
        repo_root=tmp_path,
        transcripts={(1, 2025): "dummy.txt"},
        enable_llm=True,
    )


def test_earnings_themes_degrade_on_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, qa, note = _build_themes(monkeypatch, tmp_path, _TRANSIENT)
    # The theme rollups drop to empty; the rest of §5 (deterministic) is intact.
    assert prepared == []
    assert qa == []
    assert note is None


def test_earnings_themes_propagate_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LLMBudgetExceeded):
        _build_themes(monkeypatch, tmp_path, LLMBudgetExceeded("themes cap"))


# ---------------------------------------------------------------------------
# §13 Exec compensation — previously swallowed budget caps too
# ---------------------------------------------------------------------------

_MIN_PACKAGE: dict[str, object] = {
    "executive_name": "Jane Doe",
    "role": "CEO",
    "is_ceo": 1,
    "fiscal_year": 2024,
    "currency": "USD",
}


def _build_exec_comp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException):
    # One package row enters the ``enable_llm and (rows or insider_signals)``
    # branch; the alignment-narrative generator then raises.
    monkeypatch.setattr(
        "report.sections.exec_compensation._load_packages", _returner([_MIN_PACKAGE])
    )
    monkeypatch.setattr(
        "report.sections.exec_compensation._generate_alignment_narrative", _raiser(exc)
    )
    return exec_compensation.build("NU", tmp_path, enable_llm=True)


def test_exec_comp_degrades_on_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    section = _build_exec_comp(monkeypatch, tmp_path, _TRANSIENT)
    # The narrative is dropped, but the deterministic comp rows still render.
    assert section.status in (SectionStatus.OK, SectionStatus.PARTIAL)
    assert section.alignment_narrative_md is None


def test_exec_comp_propagates_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LLMBudgetExceeded):
        _build_exec_comp(monkeypatch, tmp_path, LLMBudgetExceeded("exec_comp cap"))


# ---------------------------------------------------------------------------
# Valuation — LLM multiple-picker runs inside the compute layer
# ---------------------------------------------------------------------------


def _stub_db(tmp_path: Path) -> Path:
    """An empty file is a valid (empty) SQLite DB, so ``open_repo_db`` returns a
    connection and the build reaches the patched compute call."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "portfolio.db").write_bytes(b"")
    return tmp_path


def _build_valuation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException):
    _stub_db(tmp_path)
    monkeypatch.setattr(
        "report.sections.valuation.compute_valuation.extract_for_ticker", _raiser(exc)
    )
    return valuation.build(ticker="NU", repo_root=tmp_path, enable_llm=True)


def test_valuation_degrades_on_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    section = _build_valuation(monkeypatch, tmp_path, _TRANSIENT)
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None


def test_valuation_propagates_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LLMBudgetExceeded):
        _build_valuation(monkeypatch, tmp_path, LLMBudgetExceeded("valuation cap"))

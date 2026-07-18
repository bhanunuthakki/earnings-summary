"""integrations.wealthplan_capacity — the Phase-2 near-term cash-need band
reader (docs/design/owner_context_federation.md §3.2 "Wealthplan capacity
reader").

Hermetic: a hand-installed FAKE ``wealthplan`` package is injected into
``sys.modules`` per test (via ``monkeypatch.setitem`` — reverted
automatically), so these tests never depend on the real sibling checkout
(matching the existing ``test_import_owner_capacity.py`` convention of never
exercising the real machine files in CI). Covers: no-wealthplan degrade,
missing-plan degrade, event-scan-error degrade, "normal" band, "elevated"
band with label-only reasons (no amounts), parent-care events skipped
(age-keyed, not date-keyed), and the horizon-window boundary.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from integrations.wealthplan_capacity import (  # noqa: E402
    DEFAULT_LOOKAHEAD_DAYS,
    read_cash_need_summary,
)

_TODAY = date(2026, 7, 17)


class _Baseline:
    def __init__(self, events: list[object]) -> None:
        self.events = events


def _install_fake_wealthplan(
    monkeypatch: pytest.MonkeyPatch, *, events: list[object]
) -> types.ModuleType:
    """Install a minimal fake ``wealthplan`` package into sys.modules — the
    exact class NAMES ``wealthplan_capacity.py`` isinstance-checks against,
    with only the attributes it reads. Returns the installed ``models``
    module so callers can construct MORE events with the SAME class objects
    afterward (isinstance identity matters — re-running this installer would
    mint fresh classes and break isinstance checks against earlier instances).
    """

    class BabyEvent:
        def __init__(self, birth_date: date) -> None:
            self.birth_date = birth_date

    class BuyHouseEvent:
        def __init__(self, purchase_date: date) -> None:
            self.purchase_date = purchase_date

    class MoveCityEvent:
        def __init__(self, move_date: date) -> None:
            self.move_date = move_date

    class WorkBreakEvent:
        def __init__(self, start_date: date) -> None:
            self.start_date = start_date

    class StartupEvent:
        def __init__(self, start_date: date) -> None:
            self.start_date = start_date

    class ExitPayoutEvent:
        def __init__(self, payout_date: date) -> None:
            self.payout_date = payout_date

    class ParentCareEvent:
        def __init__(self, start_age: int = 50, end_age: int = 70) -> None:
            self.start_age = start_age
            self.end_age = end_age

    models_mod = types.ModuleType("wealthplan.models")
    for name, cls in (
        ("BabyEvent", BabyEvent),
        ("BuyHouseEvent", BuyHouseEvent),
        ("MoveCityEvent", MoveCityEvent),
        ("WorkBreakEvent", WorkBreakEvent),
        ("StartupEvent", StartupEvent),
        ("ExitPayoutEvent", ExitPayoutEvent),
        ("ParentCareEvent", ParentCareEvent),
    ):
        setattr(models_mod, name, cls)

    persistence_mod = types.ModuleType("wealthplan.persistence")

    def load_plan() -> tuple[object, _Baseline]:
        return (object(), _Baseline(events))

    persistence_mod.load_plan = load_plan  # type: ignore[attr-defined]

    wealthplan_pkg = types.ModuleType("wealthplan")
    wealthplan_pkg.models = models_mod  # type: ignore[attr-defined]
    wealthplan_pkg.persistence = persistence_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "wealthplan", wealthplan_pkg)
    monkeypatch.setitem(sys.modules, "wealthplan.models", models_mod)
    monkeypatch.setitem(sys.modules, "wealthplan.persistence", persistence_mod)
    return models_mod


def _persistence_mod() -> types.ModuleType:
    """The currently-installed fake ``wealthplan.persistence`` module, fetched
    via ``sys.modules`` rather than a static ``import wealthplan.persistence``
    — pyright has no resolution path to the real sibling package (same reason
    ``src/integrations/wealthplan_capacity.py`` itself is pyright-excluded in
    pyproject.toml), so a static import here would be an unresolved-import
    error with no real bug behind it."""
    return sys.modules["wealthplan.persistence"]


def _set_events(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> None:
    """Repoint the ALREADY-installed fake ``wealthplan.persistence.load_plan``
    at new events, without re-minting the models module — preserves class
    identity so isinstance checks in the reader still match."""

    def load_plan() -> tuple[object, _Baseline]:
        return (object(), _Baseline(events))

    monkeypatch.setattr(_persistence_mod(), "load_plan", load_plan)


@pytest.fixture(autouse=True)
def _clear_wealthplan_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure a clean slate: pop any previously cached wealthplan modules (from
    another test in the same session, or a stray import) before each test —
    monkeypatch reverts this automatically after the test."""
    for name in ("wealthplan", "wealthplan.models", "wealthplan.persistence"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield


def test_no_wealthplan_checkout_degrades(tmp_path: Path) -> None:
    """No fake installed, no real checkout on this path -> unavailable, never raises."""
    result = read_cash_need_summary(wealthplan_root=tmp_path / "no_such_wealthplan", as_of=_TODAY)
    assert result.available is False
    assert result.band is None
    assert result.reason is not None


def test_missing_plan_file_degrades(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake_wealthplan(monkeypatch, events=[])

    def _none_plan() -> None:
        return None

    monkeypatch.setattr(_persistence_mod(), "load_plan", _none_plan)
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.available is False
    assert "no plan.local.json" in (result.reason or "")


def test_load_plan_raising_degrades(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake_wealthplan(monkeypatch, events=[])

    def _raising() -> None:
        raise ValueError("boom")

    monkeypatch.setattr(_persistence_mod(), "load_plan", _raising)
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.available is False
    assert "load_plan failed" in (result.reason or "")


def test_no_events_in_window_is_normal_band(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_wealthplan(monkeypatch, events=[])
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.available is True
    assert result.band == "normal"
    assert result.reasons == ()


def test_event_within_window_is_elevated_with_label_only_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_mod = _install_fake_wealthplan(monkeypatch, events=[])

    work_break = models_mod.WorkBreakEvent(start_date=_TODAY + timedelta(days=200))
    _set_events(monkeypatch, [work_break])
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.available is True
    assert result.band == "elevated"
    assert result.reasons == ("work break",)
    # NO amounts anywhere in the result — band + label only.
    for reason in result.reasons:
        assert "$" not in reason


def test_event_far_outside_window_is_normal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_mod = _install_fake_wealthplan(monkeypatch, events=[])

    baby = models_mod.BabyEvent(birth_date=date(2031, 3, 1))
    _set_events(monkeypatch, [baby])
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.band == "normal"


def test_multiple_events_dedupe_and_all_show(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_mod = _install_fake_wealthplan(monkeypatch, events=[])

    events = [
        models_mod.WorkBreakEvent(start_date=_TODAY + timedelta(days=100)),
        models_mod.BuyHouseEvent(purchase_date=_TODAY + timedelta(days=300)),
        models_mod.WorkBreakEvent(start_date=_TODAY + timedelta(days=400)),  # dup label
    ]
    _set_events(monkeypatch, events)
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.band == "elevated"
    assert set(result.reasons) == {"work break", "home purchase"}
    assert len(result.reasons) == 2  # deduped, not 3


def test_parent_care_event_skipped_never_crashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_mod = _install_fake_wealthplan(monkeypatch, events=[])

    pc = models_mod.ParentCareEvent(start_age=70, end_age=80)
    _set_events(monkeypatch, [pc])
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.available is True
    assert result.band == "normal"  # age-keyed event contributes nothing


def test_horizon_boundary_is_inclusive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    models_mod = _install_fake_wealthplan(monkeypatch, events=[])

    boundary_date = _TODAY + timedelta(days=DEFAULT_LOOKAHEAD_DAYS)
    move = models_mod.MoveCityEvent(move_date=boundary_date)
    _set_events(monkeypatch, [move])
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY)
    assert result.band == "elevated"
    assert result.reasons == ("relocation",)


def test_custom_lookahead_days_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    models_mod = _install_fake_wealthplan(monkeypatch, events=[])

    startup = models_mod.StartupEvent(start_date=_TODAY + timedelta(days=45))
    _set_events(monkeypatch, [startup])
    result = read_cash_need_summary(wealthplan_root=tmp_path, as_of=_TODAY, lookahead_days=30)
    assert result.band == "normal"  # 45 days out, but window is only 30

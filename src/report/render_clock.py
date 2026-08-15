"""One render-scoped clock with an explicit offline override."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time


@dataclass(frozen=True)
class RenderClock:
    as_of: date

    @property
    def now_utc(self) -> datetime:
        return datetime.combine(self.as_of, time.min, tzinfo=UTC)


_CLOCK: ContextVar[RenderClock | None] = ContextVar("report_render_clock", default=None)


def render_now() -> datetime:
    clock = _CLOCK.get()
    return clock.now_utc if clock is not None else datetime.now(UTC)


def render_today() -> date:
    clock = _CLOCK.get()
    return clock.as_of if clock is not None else date.today()


def require_fixed_clock() -> RenderClock:
    clock = _CLOCK.get()
    if clock is None:
        raise RuntimeError("offline report execution requires an explicit render clock")
    return clock


@contextmanager
def fixed_render_clock(as_of: date) -> Generator[RenderClock]:
    clock = RenderClock(as_of=as_of)
    token = _CLOCK.set(clock)
    try:
        yield clock
    finally:
        _CLOCK.reset(token)


__all__ = ["RenderClock", "fixed_render_clock", "render_now", "render_today", "require_fixed_clock"]

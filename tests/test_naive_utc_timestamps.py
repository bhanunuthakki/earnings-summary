"""The naive-UTC timestamp convention and its guard.

Every TEXT timestamp column in this repo stores a *naive*-UTC ISO 8601 string;
an aware (``+00:00``) stamp crashes any aware-vs-naive ``datetime`` comparison a
consumer makes against the naive values the other stores hold. ``src/clock.py``
is the single source of truth, and this module:

  * pins clock's own contract (now_iso / now_naive_utc / to_naive_utc),
  * locks in the two formerly-aware helpers (entity_store, user_state._db),
  * proves their read paths normalize legacy aware rows so old + new coexist,
  * and ratchets against any *new* aware ``now_iso``-style helper drifting in.

See ``project_naive_utc_datetime_convention``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import clock
import entity_store
from user_state import _db as user_state_db

# entity_store's now-stamp + read-normalizer are module-private; the repo's test
# convention is to reach them with an explicit reportPrivateUsage suppression.
_entity_now_iso = entity_store._now_iso  # pyright: ignore[reportPrivateUsage]
_entity_parse_dt = entity_store._parse_dt  # pyright: ignore[reportPrivateUsage]

SRC = Path(__file__).resolve().parents[1] / "src"

# A helper that returns ``datetime.now(UTC).isoformat(...)`` emits an *aware*
# stamp — the exact landmine this convention exists to prevent. The one
# sanctioned exception is ``compute/kpi_extract_summaries.py::_now_iso_z``, which
# deliberately produces a ``...Z``-suffixed stamp because a column depends on
# that precise format; it is allow-listed here. Every other helper must route
# through ``clock.now_iso`` / ``clock.now_naive_utc`` (naive-UTC).
_AWARE_RETURN = re.compile(r"return\s+datetime\.now\(UTC\)\.isoformat\(")
_ALLOWED = {"compute/kpi_extract_summaries.py"}


def _is_naive_iso(stamp: str) -> bool:
    """An ISO string with no timezone designator (no offset, no ``Z``)."""
    return (
        "+" not in stamp
        and not stamp.endswith("Z")
        and datetime.fromisoformat(stamp).tzinfo is None
    )


# --- clock.py: the canonical helpers ----------------------------------------


def test_clock_now_helpers_are_naive() -> None:
    assert clock.now_naive_utc().tzinfo is None
    assert _is_naive_iso(clock.now_iso())


def test_clock_to_naive_utc_converts_aware_and_passes_naive_through() -> None:
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert clock.to_naive_utc(aware) == datetime(2026, 1, 1, 12, 0)
    assert clock.to_naive_utc(aware).tzinfo is None
    # A non-UTC aware stamp is converted to UTC, not merely stripped.
    from datetime import timedelta, timezone

    est = datetime(2026, 1, 1, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert clock.to_naive_utc(est) == datetime(2026, 1, 1, 12, 0)
    # Naive input is assumed UTC and passes through unchanged.
    naive = datetime(2026, 1, 1, 12, 0)
    assert clock.to_naive_utc(naive) == naive


# --- the two formerly-aware helpers -----------------------------------------


def test_formerly_aware_helpers_now_emit_naive() -> None:
    assert _is_naive_iso(_entity_now_iso())
    assert _is_naive_iso(user_state_db.now_iso())


def test_read_paths_normalize_legacy_aware_rows() -> None:
    # A row written before the flip carries an aware +00:00 offset; the read path
    # must hand it back naive (converted, not just stripped) so it compares with
    # the naive datetimes consumers use.
    aware_text = "2026-01-01T12:00:00+00:00"
    expected = datetime(2026, 1, 1, 12, 0)
    assert user_state_db.parse_dt(aware_text) == expected
    assert user_state_db.parse_dt(aware_text).tzinfo is None
    ent = _entity_parse_dt(aware_text)
    assert ent is not None
    assert ent == expected
    assert ent.tzinfo is None

    # New naive stamps round-trip unchanged.
    naive_text = "2026-01-01T12:00:00"
    assert user_state_db.parse_dt(naive_text) == expected
    assert _entity_parse_dt(naive_text) == expected

    # An already-naive datetime passed through (not a string) stays naive.
    assert user_state_db.parse_dt(expected) == expected


# --- the ratchet ------------------------------------------------------------


def test_no_new_aware_now_iso_helpers_in_src() -> None:
    offenders: dict[str, list[int]] = {}
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if not _AWARE_RETURN.search(text):
            continue
        rel = path.relative_to(SRC).as_posix()
        offenders[rel] = [
            i for i, line in enumerate(text.splitlines(), 1) if _AWARE_RETURN.search(line)
        ]

    unexpected = {rel: lines for rel, lines in offenders.items() if rel not in _ALLOWED}
    assert not unexpected, (
        "Aware `return datetime.now(UTC).isoformat(...)` helper(s) found outside the "
        "canonical clock module — these store a +00:00 offset that crashes naive-vs-aware "
        "comparisons. Route through clock.now_iso / clock.now_naive_utc instead. "
        f"Offenders: {unexpected}"
    )

"""VAL-DEDUP-001: canonical deterministic helpers are byte-identical to legacy copies.

The sprawl audit (``.tmp/audit/sprawl.md`` F3) found the deterministic helpers
that content hashes and Observation Version identities depend on redefined
privately across the tree (``_canonical_json`` x44, ``canonical_json`` x32,
``_sha256`` x43, ``_sha`` x47, ``_digest`` x29, ``_sha256_text`` x13,
``_utc`` x29, ``_aware`` x23, ``_now_iso`` x19, ...). ``src/runtime/
deterministic.py`` is the canonical module; ``docs/quality/canonical-helpers.json``
is the committed pre-migration inventory: every captured legacy definition, its
exact canonical mapping, and the disposition of every retained copy.

This suite proves, in the contract's mandatory order:

1. per-site byte identity — every mapped call site's OLD private implementation
   (captured verbatim, exec'd from the fixture) produces output byte-identical
   to its mapped canonical function over a seeded, deterministic corpus plus
   real payload/hash rows read-only from the bench DB;
2. distinct-variant discipline — genuinely different byte semantics
   (``ensure_ascii``, ``allow_nan``, ``default=str``, separators, naive-vs-aware
   coercion, raising-vs-tolerant table checks) stay distinct named functions
   and provably differ on discriminating probes;
3. tree-state enforcement — collapsed sites are absent, retained sites are
   byte-unchanged and carry a written reason, and no undispositioned private
   copy exists anywhere under ``src/`` or ``execution/``.

The bench-DB legs are mission-local evidence (they skip hermetically when
``.tmp/portfolio_bench.db`` is absent). The bench DB is only ever opened with
``mode=ro``.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Self, cast

import pytest
from pydantic import BaseModel

import clock
from provenance.population_cli_harness import parse_timezone_aware_datetime
from runtime import deterministic

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "docs" / "quality" / "canonical-helpers.json"
BENCH_DB = PROJECT_ROOT / ".tmp" / "portfolio_bench.db"
SEED = 20261009

FixtureSite = dict[str, object]


# ---------------------------------------------------------------------------
# fixture + canonical target resolution
# ---------------------------------------------------------------------------


def _load_fixture() -> dict[str, object]:
    fixture = cast("dict[str, object]", json.loads(FIXTURE_PATH.read_text()))
    assert fixture["schema_version"] == "1"
    return fixture


FIXTURE = _load_fixture()
SITES = cast("list[FixtureSite]", FIXTURE["sites"])


# Static tables (not getattr/importlib) keep this suite out of the
# operational-reachability scanner's dynamic-reference count.
_OUTSIDE_CANONICAL_TARGETS: dict[str, Callable[..., object]] = {
    "clock.now_iso": clock.now_iso,
    "clock.now_naive_utc": clock.now_naive_utc,
    "clock.now_iso_space_seconds": clock.now_iso_space_seconds,
    "provenance.population_cli_harness.parse_timezone_aware_datetime": (
        parse_timezone_aware_datetime
    ),
}

# Every module-level import a captured legacy body references, by (module, name).
_REFERENCED_IMPORTS: dict[tuple[str, str], object] = {
    ("clock", "now_iso"): clock.now_iso,
    ("clock", "now_naive_utc"): clock.now_naive_utc,
    ("hashlib", "sha256"): hashlib.sha256,
    ("provenance.population_cli_harness", "parse_timezone_aware_datetime"): (
        parse_timezone_aware_datetime
    ),
    ("pydantic", "BaseModel"): BaseModel,
}


def _canonical_callable(target: str) -> Callable[..., object]:
    if target in _OUTSIDE_CANONICAL_TARGETS:
        return _OUTSIDE_CANONICAL_TARGETS[target]
    assert target in deterministic.__all__, target
    return cast("Callable[..., object]", vars(deterministic)[target])


def _site_id(site: FixtureSite) -> str:
    return f"{site['file']}:{site['line']}:{site['name']}"


def _mapped_sites() -> list[FixtureSite]:
    return [
        site
        for site in SITES
        if site["canonical_target"] is not None
        and cast("str", site["file"]).startswith(("src/", "execution/"))
    ]


def _in_scope_sites() -> list[FixtureSite]:
    return [site for site in SITES if cast("str", site["file"]).startswith(("src/", "execution/"))]


# ---------------------------------------------------------------------------
# seeded corpora (deterministic by construction)
# ---------------------------------------------------------------------------

_FIXED_SCALARS: list[object] = [
    None,
    True,
    False,
    0,
    -1,
    2**63,
    0.1,
    1e300,
    -0.0,
    float("inf"),
    float("nan"),
    "",
    "ascii",
    "café",
    "日本語",
    'quote"back\\slash',
    "\ud800",
    "𝄞 music",
]


def _generated_values(count: int, rng: random.Random) -> list[object]:
    """Seeded nested structures over a representative palette."""

    palette: list[object] = [
        None,
        True,
        False,
        0,
        1,
        -7,
        1234567890123,
        0.1,
        -0.0,
        1e-8,
        "",
        "x",
        "value with spaces",
        "café",
        "ünïcödé",
        "日本語テキスト",
        "tab\tvalue",
        "quote'd",
        'double"quoted',
        "\ud800",
    ]

    def build(depth: int) -> object:
        if depth <= 0:
            return rng.choice(palette)
        kind = rng.randint(0, 6)
        if kind == 0:
            return {f"key{rng.randint(0, 9)}": build(depth - 1) for _ in range(rng.randint(0, 5))}
        if kind == 1:
            return [build(depth - 1) for _ in range(rng.randint(0, 5))]
        if kind == 2:
            empty: dict[str, object] = {}
            return {"": build(depth - 1), "z": None, "a": [1, 2, empty]}
        if kind == 3:
            return {"nested": {"deep": {"deeper": build(depth - 1)}}, "flat": 1}
        if kind == 4:
            return {"a": 0.1, "b": [1e300, -0.0], "c": "café"}
        if kind == 5:
            empty2: dict[str, object] = {}
            return [{"t": "café"}, {"t": "日本語"}, empty2]
        return rng.choice(palette)

    return [build(rng.randint(1, 4)) for _ in range(count)]


JSON_CORPUS: list[object] = _FIXED_SCALARS + _generated_values(260, random.Random(SEED))

TEXT_PROBES: list[str] = [
    "",
    "hello",
    "café",
    "日本語",
    "\ud800",
    "x" * 512,
    "line\nbreak",
    "tab\there",
    "\x00\x01\x02",
]

BYTES_PROBES: list[bytes] = [
    b"",
    b"abc",
    bytes(range(256)),
    b"\x00" * 2048,
    "café".encode(),
]

JOINED_PROBES: list[tuple[str, ...]] = [
    ("a",),
    ("a", "b"),
    ("", ""),
    ("café", "日本語"),
    ("x" * 10, "y" * 10, "z" * 10),
    (),
    ("with\nnewline", "with\x1fsep", "with\0nul"),
]

DIGEST_PROBES: list[str] = [
    "0123456789abcdef" * 4,
    ("0123456789ABCDEF" * 4).lower(),
    "0123456789ABCDEF" * 4,
    "0123456789abcde" * 4,
    "z" * 64,
    "",
    "0123456789abcdef" * 4 + "x",
    "𝄞" * 64,
]

_DT_BASE = datetime(2026, 10, 9, 12, 34, 56, 789123)
IST = timezone(timedelta(hours=5, minutes=30))

DATETIME_PROBES: list[object] = [
    _DT_BASE,
    datetime(2026, 10, 9, 12, 34, 56),
    _DT_BASE.replace(tzinfo=UTC),
    _DT_BASE.replace(tzinfo=IST),
    datetime(2020, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=-5), "EST"), fold=0),
    datetime(2020, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=-5), "EST"), fold=1),
    datetime(1970, 1, 1),
    datetime(9999, 12, 31, 23, 59, 59, 999999),
]

DATETIME_TEXT_PROBES: list[object] = [
    "2026-10-09T12:34:56",
    "2026-10-09T12:34:56.789123",
    "2026-10-09T12:34:56Z",
    "2026-10-09T12:34:56+00:00",
    "2026-10-09T12:34:56+05:30",
    "2026-10-09 12:34:56",
    "2026-10-09",
    "",
    "garbage",
    None,
    12345,
    date(2026, 10, 9),
    datetime(2026, 10, 9, 12, 34, 56),
    datetime(2020, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=-5), "EST"), fold=1),
]

PARSER_PROBES: list[object] = DATETIME_PROBES + DATETIME_TEXT_PROBES

DATE_TEXT_PROBES: list[object] = [
    "2026-10-09",
    "2026-10-09T12:34:56",
    "2026-10-09 12:34:56",
    "2026-10",
    "garbage",
    "",
    None,
    12345,
    date(2026, 10, 9),
    datetime(2026, 10, 9, 12, 0, 0),
    "2026-10-09T12:34:56+05:30",
]


class _FrozenDateTimeMeta(type):
    """Make ``isinstance(x, _FrozenDateTime)`` behave exactly like ``datetime``.

    The exec namespace binds the name ``datetime`` to this subclass so legacy
    bodies call ``datetime.now`` deterministically; their ``isinstance`` checks
    must keep accepting every real ``datetime``, not just this subclass.
    """

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)


class _FrozenDateTime(datetime, metaclass=_FrozenDateTimeMeta):
    """datetime subclass whose now() is fixed (for deterministic clock proofs)."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        fixed = cast("Self", _FROZEN_AWARE)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)


_FROZEN_AWARE = datetime(2026, 10, 9, 8, 0, 0, 123456, tzinfo=UTC)


class _ProbeRoot(BaseModel):
    root: object


class _ProbeModel(BaseModel):
    ticker: str
    count: int
    note: str | None = None


class _ProbeExcludeNone(BaseModel):
    ticker: str
    note: str | None = None
    tags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# exec machinery for captured legacy implementations
# ---------------------------------------------------------------------------


def _base_namespace() -> dict[str, object]:
    import argparse
    import re as re_module
    import sys
    import time
    import typing

    ns: dict[str, object] = {
        "__name__": "canonical_helpers_fixture_namespace",
        "json": json,
        "hashlib": hashlib,
        "re": re_module,
        "argparse": argparse,
        "sqlite3": sqlite3,
        "datetime": _FrozenDateTime,
        "date": date,
        "timedelta": timedelta,
        "tzinfo": tzinfo,
        "time": time,
        "timezone": timezone,
        "UTC": UTC,
        "Path": Path,
        "BaseModel": BaseModel,
        "sha256": hashlib.sha256,
        "str": str,
        "int": int,
        "bool": bool,
        "bytes": bytes,
        "float": float,
        "list": list,
        "dict": dict,
        "tuple": tuple,
        "set": set,
        "object": object,
        "Self": typing.Self,
        "JsonValue": object,
        "Sequence": typing.Sequence if hasattr(typing, "Sequence") else list,
        "Callable": typing.Callable,
        "Iterable": typing.Iterable,
        "Optional": typing.Optional,
        "sys": sys,
    }
    ns["now_iso"] = clock.now_iso
    ns["now_naive_utc"] = clock.now_naive_utc
    ns["to_naive_utc"] = clock.to_naive_utc
    ns["now_iso_space_seconds"] = clock.now_iso_space_seconds
    ns["parse_timezone_aware_datetime"] = parse_timezone_aware_datetime
    return ns


def _exec_site(site: FixtureSite, ns: dict[str, object]) -> Callable[..., object]:
    referenced = cast("dict[str, dict[str, str]]", site["referenced"])
    for name, ref in referenced.items():
        if "source" in ref:
            exec(ref["source"], ns)
        else:
            import_key = (ref["import_module"], ref["import_name"])
            assert import_key in _REFERENCED_IMPORTS, f"unmapped import {import_key}"
            ns[name] = _REFERENCED_IMPORTS[import_key]
    exec(cast("str", site["source"]), ns)
    fn = ns.get(cast("str", site["name"]))
    if not callable(fn):
        raise AssertionError(f"fixture site did not define {site['name']}: {_site_id(site)}")
    return fn


def _outcome(
    fn: Callable[..., object],
    args: tuple[object, ...],
    kwargs: dict[str, object] | None = None,
) -> tuple[bool, object, type[BaseException] | None]:
    """Call fn, reducing outcomes to (ok, value) or (raised, exception type)."""
    try:
        return (True, fn(*args, **(kwargs or {})), None)
    except Exception as exc:
        return (False, None, type(exc))


def _outcomes_equal(
    left: tuple[bool, object, type[BaseException] | None],
    right: tuple[bool, object, type[BaseException] | None],
) -> bool:
    if left[0] != right[0]:
        return False
    if left[0]:
        lv: object = left[1]
        rv: object = right[1]
        if isinstance(lv, datetime) and isinstance(rv, datetime):
            # The exec harness constructs legacy datetimes through
            # _FrozenDateTime (so datetime.now is deterministic); the harness
            # class is not part of the semantics — compare the instant itself
            # including tzinfo and fold.
            return lv == rv and lv.tzinfo == rv.tzinfo and lv.fold == rv.fold
        return type(lv) is type(rv) and lv == rv
    return left[2] is right[2]


def _probe_args(
    target: str, extra: dict[str, object] | None, files: list[Path], conn: sqlite3.Connection
) -> list[tuple[object, ...]]:
    if target.startswith("canonical_json"):
        return [(value,) for value in JSON_CORPUS]
    if target == "sha256_text":
        return [(value,) for value in TEXT_PROBES]
    if target == "sha256_bytes":
        return [(value,) for value in BYTES_PROBES]
    if target == "sha256_file":
        return [(path,) for path in files]
    if target.startswith("sha256_joined"):
        return list(JOINED_PROBES)
    if target.startswith("content_sha256") and target.endswith("_or_text"):
        return [(value,) for value in JSON_CORPUS + TEXT_PROBES]
    if target.startswith("content_sha256"):
        return [(value,) for value in JSON_CORPUS]
    if target in ("require_sha256", "require_sha256_exact"):
        # An f-string message template means the legacy validator took the
        # field name as a second argument (interpolated into the message).
        message = cast("str", (extra or {}).get("message"))
        if message.startswith("f'"):
            return [(value, "probe_field") for value in DIGEST_PROBES]
        return [(value,) for value in DIGEST_PROBES]
    if target == "as_utc":
        return [(value,) for value in DATETIME_PROBES]
    if target in ("parse_instant", "parse_iso8601_utc"):
        return [(value,) for value in PARSER_PROBES]
    if target == "parse_iso8601_cli":
        return [(value,) for value in DATETIME_TEXT_PROBES]
    if target == "parse_iso8601_named":
        return [(value, "probe_field") for value in PARSER_PROBES]
    if target == "parse_date_text_or_none":
        return [(value,) for value in DATE_TEXT_PROBES]
    if target.startswith("clock."):
        return [()]
    if target.startswith("provenance."):
        return [(value,) for value in PARSER_PROBES]
    if target in ("table_exists", "table_exists_or_false", "table_or_view_exists"):
        if extra is not None and extra.get("default_table") is not None:
            # Fixed/default table-name checks: the legacy function folds the
            # table into its body or a default argument, so it takes only conn.
            return [(conn,)]
        return [
            (conn, "probe_table"),
            (conn, "missing_table"),
            (conn, "probe_view"),
            (conn, ""),
        ]
    raise AssertionError(f"no probe adapter for canonical target {target}")


def _canonical_call_args(
    target: str, extra: dict[str, object] | None, args: tuple[object, ...]
) -> tuple[tuple[object, ...], dict[str, object]]:
    if target in ("require_sha256", "require_sha256_exact") and extra is not None:
        message_template = cast("str", extra.get("message"))
        if message_template.startswith("f'"):
            # f-string message: legacy took (value, name); evaluate the template
            # against the probe's field name.
            return (args[0],), {"message": message_template[2:-1].format(name=args[1])}
        return args, {"message": message_template}
    if target in ("table_exists", "table_exists_or_false", "table_or_view_exists"):
        if extra is not None and extra.get("default_table") is not None and len(args) == 1:
            return (args[0], cast("str", extra["default_table"])), {}
        return args, {}
    if target.startswith("clock."):
        return (), {}
    if target == "parse_iso8601_named":
        return args, {}
    return args, {}


@pytest.fixture(scope="module")
def probe_files(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    directory = tmp_path_factory.mktemp("canonical-helper-files")
    paths: list[Path] = []
    empty = directory / "empty.bin"
    empty.write_bytes(b"")
    paths.append(empty)
    small = directory / "small.txt"
    small.write_bytes(b"deterministic payload\n")
    paths.append(small)
    binary = directory / "binary.bin"
    binary.write_bytes(bytes(range(256)) * 4)
    paths.append(binary)
    big = directory / "big.bin"
    big.write_bytes(b"A" * (1024 * 1024 + 3) + b"\x00" + b"B" * (1024 * 1024 + 7))
    paths.append(big)
    return paths


@pytest.fixture(scope="module")
def probe_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE probe_table (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE second_probe (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE VIEW probe_view AS SELECT id FROM probe_table")
    # fixed-table-name checks in the fixture look for these exact tables
    conn.execute("CREATE TABLE fact_overrides (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE segment_quarterly_coverage (id INTEGER PRIMARY KEY)")
    return conn


# ---------------------------------------------------------------------------
# 1. per-site byte identity over the corpus
# ---------------------------------------------------------------------------


def test_every_canonical_target_resolves() -> None:
    for site in _mapped_sites():
        target = cast("str", site["canonical_target"])
        assert callable(_canonical_callable(target)), _site_id(site)


def test_mapped_sites_are_executable_from_the_fixture() -> None:
    ns = _base_namespace()
    for site in _mapped_sites():
        assert not site["unresolved"], f"unresolved references: {_site_id(site)}"
        _exec_site(site, dict(ns))


def test_mapped_legacy_implementations_are_byte_identical(
    probe_files: list[Path], probe_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Freeze the canonical clock's datetime so clock-target comparisons are
    # deterministic (the legacy side runs against _FrozenDateTime via the
    # exec namespace; delegating legacy bodies call clock.now_* themselves).
    monkeypatch.setattr(clock, "datetime", _FrozenDateTime)
    ns = _base_namespace()
    mismatches: list[str] = []
    checked = 0
    for site in _mapped_sites():
        scope = cast("str", site["scope"])
        if scope == "method":
            continue  # method bodies are proven by test_mapped_method_bodies_match_canonical
        target = cast("str", site["canonical_target"])
        canonical = _canonical_callable(target)
        extra = cast("dict[str, object] | None", site["canonical_extra"])
        legacy = _exec_site(site, dict(ns))
        probes = _probe_args(target, extra, probe_files, probe_conn)
        for args in probes:
            call_args, kwargs = _canonical_call_args(target, extra, args)
            legacy_outcome = _outcome(legacy, args)
            canonical_outcome = _outcome(canonical, call_args, kwargs)
            checked += 1
            if not _outcomes_equal(legacy_outcome, canonical_outcome):
                mismatches.append(
                    f"{_site_id(site)} -> {target} probe={args!r} "
                    f"legacy={legacy_outcome!r} canonical={canonical_outcome!r}"
                )
                if len(mismatches) >= 25:
                    break
        if len(mismatches) >= 25:
            break
    assert not mismatches, "\n".join(mismatches)
    assert checked > 200, f"expected a broad corpus sweep, checked {checked} probes"


def test_mapped_method_bodies_match_canonical() -> None:
    """One-expression canonical_json(self) methods delegate to the exact variant."""
    ns = _base_namespace()
    standard_probes: list[BaseModel] = [
        _ProbeModel(ticker="NU", count=3),
        _ProbeExcludeNone(ticker="NU", note=None),
    ]
    # RootModel-shaped sites reference self.root; only root-bearing probes can
    # exercise that form (the fixture's one such site is CanonicalJSONObject).
    root_probes: list[BaseModel] = [_ProbeRoot(root={"k": 1, "s": "café"})]
    for site in _mapped_sites():
        if cast("str", site["scope"]) != "method":
            continue
        target = cast("str", site["canonical_target"])
        extra = cast("dict[str, object] | None", site["canonical_extra"])
        assert extra is not None and "model_call" in extra, _site_id(site)
        model_call = cast("str", extra["model_call"])
        canonical = _canonical_callable(target)
        method = _exec_site(site, dict(ns))
        probes = root_probes if model_call == "self.root" else standard_probes
        for probe in probes:
            legacy_outcome = _outcome(method, (probe,))
            expected_payload: object = eval(model_call, {"self": probe})
            canonical_outcome = _outcome(canonical, (expected_payload,))
            assert _outcomes_equal(legacy_outcome, canonical_outcome), (
                f"{_site_id(site)} probe={probe!r} legacy={legacy_outcome!r} "
                f"canonical={canonical_outcome!r}"
            )


# ---------------------------------------------------------------------------
# 2. distinct-variant discipline (no silent merges)
# ---------------------------------------------------------------------------


def test_json_variants_stay_byte_distinct() -> None:
    non_ascii = {"k": "café"}
    assert deterministic.canonical_json(non_ascii) != deterministic.canonical_json_ascii(non_ascii)
    assert deterministic.canonical_json(non_ascii) == json.dumps(
        non_ascii, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert deterministic.canonical_json_ascii(non_ascii) == json.dumps(
        non_ascii, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    nan_payload = {"v": float("nan")}
    with pytest.raises(ValueError):
        deterministic.canonical_json_no_nan(nan_payload)
    with pytest.raises(ValueError):
        deterministic.canonical_json_ascii_no_nan(nan_payload)
    assert deterministic.canonical_json(nan_payload).startswith('{"v":NaN')
    non_serializable = {"s": {1, 2}}
    with pytest.raises(TypeError):
        deterministic.canonical_json(non_serializable)
    assert deterministic.canonical_json_ascii_default_str(non_serializable) == json.dumps(
        non_serializable, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True
    )
    model = _ProbeModel(ticker="NU", count=1)
    assert deterministic.canonical_json_model(model) == deterministic.canonical_json(
        model.model_dump(mode="json")
    )


def test_sha_variants_stay_byte_distinct() -> None:
    text = "café"
    assert deterministic.sha256_text(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    raw = text.encode("utf-8")
    assert deterministic.sha256_bytes(raw) == hashlib.sha256(raw).hexdigest()
    assert deterministic.sha256_joined_unit_sep("a", "b") != deterministic.sha256_joined_nul(
        "a", "b"
    )
    assert (
        deterministic.sha256_joined_unit_sep("a", "b")
        == hashlib.sha256("\x1f".join(("a", "b")).encode()).hexdigest()
    )
    assert deterministic.content_sha256({"k": "café"}) == deterministic.sha256_text(
        deterministic.canonical_json({"k": "café"})
    )
    assert deterministic.content_sha256({"k": "café"}) != deterministic.content_sha256_ascii(
        {"k": "café"}
    )
    valid = "0123456789abcdef" * 4
    upper = valid.upper()
    assert deterministic.require_sha256(upper, message="probe") == valid
    with pytest.raises(ValueError, match="probe"):
        deterministic.require_sha256_exact(upper, message="probe")
    assert deterministic.content_sha256_or_text("plain") == deterministic.sha256_text("plain")
    assert deterministic.content_sha256_or_text({"a": 1}) == deterministic.content_sha256({"a": 1})


def test_time_variants_stay_byte_distinct() -> None:
    naive = datetime(2026, 10, 9, 12, 0, 0)
    assert deterministic.as_utc(naive) == naive.replace(tzinfo=UTC)
    assert deterministic.as_utc(naive).tzinfo is not None
    assert clock.to_naive_utc(deterministic.as_utc(naive)) == naive
    ist_stamp = datetime(2026, 10, 9, 12, 34, 56, tzinfo=IST)
    # the instant parsers coerce to aware UTC (shortcut and str-reparse agree
    # on every real instant — astimezone itself drops fold for a UTC target)
    assert deterministic.parse_instant(ist_stamp).tzinfo == UTC
    assert deterministic.parse_iso8601_utc(ist_stamp) == deterministic.parse_instant(ist_stamp)
    assert deterministic.parse_iso8601_utc(
        "2026-10-09T12:34:56Z"
    ) == deterministic.parse_iso8601_utc("2026-10-09T12:34:56+00:00")
    # ...while the CLI parser keeps the parsed offset (NO UTC coercion) and
    # raises argparse's ArgumentTypeError rather than ValueError
    assert deterministic.parse_iso8601_cli("2026-10-09T12:34:56+05:30") == ist_stamp
    with pytest.raises(Exception) as excinfo:
        deterministic.parse_iso8601_cli("garbage")
    assert excinfo.value.__class__.__name__ == "ArgumentTypeError"
    # ...and the named parser passes datetimes through UNcoerced, failing
    # closed with a field-named RuntimeError on non-text input
    assert deterministic.parse_iso8601_named(ist_stamp, "probe_field") == ist_stamp
    with pytest.raises(RuntimeError, match="probe_field must be ISO-8601"):
        deterministic.parse_iso8601_named("garbage", "probe_field")
    with pytest.raises(RuntimeError, match="probe_field must be a datetime"):
        deterministic.parse_iso8601_named(12345, "probe_field")
    assert deterministic.parse_date_text_or_none("2026-10-09T12:34:56") == date(2026, 10, 9)
    assert deterministic.parse_date_text_or_none("garbage") is None


def test_table_check_policies_stay_distinct(probe_conn: sqlite3.Connection) -> None:
    assert deterministic.table_exists(probe_conn, "probe_table")
    assert not deterministic.table_exists(probe_conn, "probe_view")
    assert deterministic.table_or_view_exists(probe_conn, "probe_view")
    assert deterministic.table_exists_or_false(probe_conn, "probe_table")
    closed = sqlite3.connect(":memory:")
    closed.close()
    with pytest.raises(sqlite3.ProgrammingError):
        deterministic.table_exists(closed, "probe_table")
    assert deterministic.table_exists_or_false(closed, "probe_table") is False


# ---------------------------------------------------------------------------
# 3. tree-state enforcement
# ---------------------------------------------------------------------------


def _function_sources(path: Path) -> dict[str, list[str]]:
    """Map every def name (module, method, or nested) to its source segments."""
    import ast

    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    sources: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sources.setdefault(node.name, []).append(ast.get_source_segment(text, node) or "")
    return sources


def _module_function_names(path: Path) -> set[str]:
    """Names of module-level defs only (the strays the dedup contract owns)."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


FAMILY_NAMES = (
    "_canonical_json",
    "canonical_json",
    "_sha256",
    "_sha256_text",
    "_digest",
    "_sha",
    "_sha256_hex",
    "_utc",
    "_aware",
    "_datetime",
    "_now_iso",
    "_now_utc",
    "_parse_datetime",
    "_parse_date",
    "_table_exists",
    "_has_table",
    "_resolve_tickers",
)


def test_tree_state_matches_every_disposition() -> None:
    """Pre-migration: every disposition=live/retained site is present verbatim.

    Post-migration: collapsed sites' legacy definitions are gone (name+source
    pair absent), retained sites stay byte-unchanged.
    """
    problems: list[str] = []
    for site in _in_scope_sites():
        path = PROJECT_ROOT / cast("str", site["file"])
        if not path.exists():
            problems.append(f"missing file: {site['file']}")
            continue
        segments = _function_sources(path).get(cast("str", site["name"]), [])
        source = cast("str", site["source"])
        disposition = cast("str", site["disposition"])
        if disposition in ("live", "retained"):
            if source not in segments:
                problems.append(
                    f"{_site_id(site)} disposition={disposition} but definition is absent or drifted"
                )
        elif disposition == "collapsed":
            if source in segments:
                problems.append(
                    f"{_site_id(site)} disposition=collapsed but the legacy definition is still present"
                )
        else:
            problems.append(f"{_site_id(site)} unknown disposition {disposition}")
    assert not problems, "\n".join(problems)


CANONICAL_MODULE = "src/runtime/deterministic.py"


def test_no_undispositioned_module_definitions() -> None:
    """No family-named private copy may exist outside the fixture and the canonical module."""
    known = {(cast("str", site["file"]), cast("str", site["name"])) for site in _in_scope_sites()}
    strays: list[str] = []
    for base in ("src", "execution"):
        for path in (PROJECT_ROOT / base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if rel == CANONICAL_MODULE:
                continue  # the canonical home is allowed to define the family names
            for name in _module_function_names(path):
                if name in FAMILY_NAMES and (rel, name) not in known:
                    strays.append(f"{rel}: {name}")
    assert not strays, "undispositioned private helper copies:\n" + "\n".join(strays)


def test_retained_sites_carry_written_reasons() -> None:
    for site in _in_scope_sites():
        if cast("str", site["disposition"]) == "retained":
            reason = cast("str | None", site["retained_reason"])
            assert reason and len(reason) > 20, (
                f"retained without a written reason: {_site_id(site)}"
            )


def test_fixture_summary_is_consistent() -> None:
    summary = cast("dict[str, dict[str, int]]", FIXTURE["summary"])
    for family, counts in summary.items():
        sites = [site for site in SITES if site["family"] == family]
        assert counts["total"] == len(sites), family
        mapped = [site for site in sites if site["canonical_target"] is not None]
        assert counts["mappable"] == len(mapped), family
        for disposition in ("collapsed", "retained"):
            matching = [site for site in sites if site["disposition"] == disposition]
            assert counts[disposition] == len(matching), (family, disposition)
    assert not [site for site in SITES if site["disposition"] == "live"], (
        "every site is either collapsed onto the canonical module or retained with a reason"
    )


# ---------------------------------------------------------------------------
# 4. bench-DB legs: stored-hash non-drift on real rows (read-only)
# ---------------------------------------------------------------------------

_BENCH_SPECS: list[dict[str, object]] = [
    {
        "table": "earnings_surprise_observations",
        "payload": "canonical_payload_json",
        "hash": "canonical_payload_sha256",
        "json_target": "canonical_json",
        "roundtrip": True,
        "limit": 1200,
    },
    {
        "table": "earnings_surprise_observations",
        "payload": "raw_payload_json",
        "hash": "raw_payload_sha256",
        "json_target": None,
        "roundtrip": False,
        "limit": 500,
    },
    {
        "table": "thesis_evaluation_episodes",
        "payload": "semantic_input_json",
        "hash": "semantic_input_sha256",
        "json_target": "canonical_json_no_nan",
        "roundtrip": True,
    },
    {
        "table": "owner_decision_checkpoints",
        "payload": "payload_json",
        "hash": "payload_sha256",
        "json_target": "canonical_json_no_nan",
        "roundtrip": True,
    },
    {
        "table": "ask_grounding_traces",
        "payload": "item_set_json",
        "hash": "item_set_sha256",
        "json_target": "canonical_json_ascii",
        "roundtrip": True,
    },
    {
        "table": "filing_section_items",
        "payload": "body",
        "hash": "body_sha256",
        "json_target": None,
        "roundtrip": False,
    },
    {
        "table": "issuer_fact_coverage_receipts",
        "payload": "receipt_json",
        "hash": "receipt_sha256",
        "json_target": "canonical_json",
        "roundtrip": True,
        "strip_key": "receipt_sha256",
    },
]


def _sample_rows(
    conn: sqlite3.Connection, table: str, payload: str, hash_col: str, limit: int
) -> list[tuple[str | None, str | None]]:
    return cast(
        "list[tuple[str | None, str | None]]",
        conn.execute(
            f"SELECT {payload}, {hash_col} FROM {table} "
            f"WHERE {payload} IS NOT NULL AND {hash_col} IS NOT NULL ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall(),
    )


@pytest.mark.skipif(
    not BENCH_DB.exists(), reason="bench DB not provisioned (mission-local artifact)"
)
def test_stored_payload_hashes_reproduce_with_canonical_helpers() -> None:
    conn = sqlite3.connect(f"file:{BENCH_DB}?mode=ro", uri=True)
    try:
        problems: list[str] = []
        totals: dict[str, int] = {}
        for spec in _BENCH_SPECS:
            table = cast("str", spec["table"])
            payload_column = cast("str", spec["payload"])
            rows = _sample_rows(
                conn,
                table,
                payload_column,
                cast("str", spec["hash"]),
                cast("int", spec.get("limit", 500)),
            )
            totals[f"{table}.{payload_column}"] = len(rows)
            json_target = cast("str | None", spec["json_target"])
            strip_key = cast("str | None", spec.get("strip_key"))
            for payload_text, stored_hash in rows:
                assert isinstance(payload_text, str) and isinstance(stored_hash, str)
                recomputed = deterministic.sha256_text(payload_text)
                if recomputed != stored_hash:
                    problems.append(f"{table}.{spec['hash']} text-hash mismatch on a real row")
                    continue
                if json_target is None:
                    continue
                payload_value: object = json.loads(payload_text)
                if strip_key is not None and isinstance(payload_value, dict):
                    # JSON object keys are strings; the unsigned receipt drops
                    # the embedded signature key before re-hashing.
                    payload_value = {
                        key: value
                        for key, value in cast("dict[str, object]", payload_value).items()
                        if key != strip_key
                    }
                canonical_fn = _canonical_callable(json_target)
                re_serialized = cast("str", canonical_fn(payload_value))
                if spec["roundtrip"] and strip_key is None and re_serialized != payload_text:
                    problems.append(f"{table}.{spec['payload']} roundtrip drift on a real row")
                    continue
                if deterministic.sha256_text(re_serialized) != stored_hash and strip_key is None:
                    problems.append(f"{table}.{spec['hash']} composition mismatch on a real row")
                if (
                    strip_key is not None
                    and deterministic.sha256_text(re_serialized) != stored_hash
                ):
                    problems.append(
                        f"{table}.{spec['hash']} unsigned-receipt mismatch on a real row"
                    )
        assert not problems, "\n".join(problems)
        assert totals["earnings_surprise_observations.canonical_payload_json"] > 1000
        assert totals["thesis_evaluation_episodes.semantic_input_json"] > 100
    finally:
        conn.close()


@pytest.mark.skipif(
    not BENCH_DB.exists(), reason="bench DB not provisioned (mission-local artifact)"
)
def test_observation_identity_recomputes_exactly() -> None:
    """Stored Observation Version identities must not drift through the canonical helpers."""
    conn = sqlite3.connect(f"file:{BENCH_DB}?mode=ro", uri=True)
    try:
        rows = cast(
            "list[tuple[str, str]]",
            conn.execute(
                "SELECT observation_id, canonical_payload_json FROM earnings_surprise_observations "
                "WHERE canonical_payload_json IS NOT NULL ORDER BY rowid LIMIT 1000"
            ).fetchall(),
        )
        assert len(rows) > 100, "expected real observation rows in the bench DB"
        problems: list[str] = []
        for observation_id, payload_text in rows:
            recomputed = deterministic.content_sha256(json.loads(payload_text))
            if recomputed != observation_id:
                problems.append(f"observation identity drift for {observation_id}")
                if len(problems) > 10:
                    break
        assert not problems, "\n".join(problems)
    finally:
        conn.close()

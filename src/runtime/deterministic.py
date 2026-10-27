"""Canonical deterministic helpers: stable JSON, SHA-256 digests, UTC coercion.

This module is the single canonical home for the byte-level primitives that
stored identities depend on (content hashes, Observation Version digests,
receipt fingerprints). Before it existed the same 2–5 line helpers were
redefined privately across the tree (``_canonical_json`` x44,
``canonical_json`` x32, ``_sha256`` x43, ``_sha`` x47, ``_digest`` x29,
``_sha256_text`` x13, ``_utc`` x29, ``_aware`` x23, ``_now_iso`` x19, ...) —
each copy a chance for a separator or ``ensure_ascii`` difference to change a
digest and silently break dedup, supersession, or reconstruction.

Variant discipline (load-bearing, see ``docs/quality/canonical-helpers.json``):
where legacy copies genuinely differed in byte semantics the difference is a
*distinct named function* here — ``ensure_ascii=True`` vs ``False``,
``allow_nan=False``, ``default=str``, separator-joined digests, naive-vs-aware
coercion — and every migrated call site maps to its exact variant. Never merge
two variants under one name; never add a keyword-only escape hatch that lets a
caller silently pick the wrong byte semantics. The equivalence test
(``tests/test_canonical_helpers_equivalence.py``) proves each migrated site's
old private implementation byte-identical against its mapped function.

Scope note: the *clock* stamps (``now_naive_utc``/``now_iso``/
``to_naive_utc``/``now_iso_space_seconds``) stay in ``src/clock.py``, the
pre-existing single source of truth for the naive-UTC TEXT-timestamp
convention; this module owns value coercion (aware-UTC normalization and
ISO-8601 parsing), not the clock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel

__all__ = [
    "JSON_FENCE_RX",
    "as_utc",
    "canonical_json",
    "canonical_json_ascii",
    "canonical_json_ascii_default_str",
    "canonical_json_ascii_no_nan",
    "canonical_json_model",
    "canonical_json_no_nan",
    "content_sha256",
    "content_sha256_ascii",
    "content_sha256_ascii_default_str",
    "content_sha256_ascii_no_nan",
    "content_sha256_ascii_or_text",
    "content_sha256_model",
    "content_sha256_model_or_text",
    "content_sha256_no_nan",
    "content_sha256_or_text",
    "first_json_value",
    "normalize_extracted_text",
    "parse_date_text_or_none",
    "parse_instant",
    "parse_iso8601_cli",
    "parse_iso8601_named",
    "parse_iso8601_utc",
    "parse_json_payload",
    "require_sha256",
    "require_sha256_exact",
    "sha256_bytes",
    "sha256_file",
    "sha256_joined_nul",
    "sha256_joined_unit_sep",
    "sha256_text",
    "table_exists",
    "table_exists_or_false",
    "table_or_view_exists",
]


# --------------------------------------------------------------------------
# Stable JSON — one named function per byte-distinct legacy variant.
# --------------------------------------------------------------------------


JSON_FENCE_RX = re.compile(r"^```(?:json)?\s*|\s*```$")


def first_json_value(text: str, expect: Literal["object", "array"]) -> object:
    """Decode the first complete JSON value of the expected shape from ``text``,
    ignoring any trailing prose/fences the model appended after it.

    ``raw_decode`` parses one value and reports where it ended, so a chatty
    ``[]\\n```\\n\\n<explanation>`` yields ``[]``. Raises ValueError (naming the
    problem) when no plausible opener is present or the value won't decode — the
    loud contract is preserved."""
    opener = "{" if expect == "object" else "["
    idx = text.find(opener)
    if idx == -1:
        raise ValueError(f"not valid JSON: no {expect} opener found in response")
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[idx:])
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    return payload


def parse_json_payload(
    raw: str,
    *,
    expect: Literal["object", "array"] = "object",
    required_keys: tuple[str, ...] = (),
) -> object:
    """Fence-tolerant strict parse. Raises ValueError naming the problem when
    the text isn't the expected JSON shape or misses a required key."""
    text = raw.strip()
    if text.startswith("```"):
        text = JSON_FENCE_RX.sub("", text).strip()
    try:
        payload: object = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Tolerate trailing prose/fences after an otherwise-valid JSON value: a
        # chatty model emits ``[]\n```\n\nThe section describes ...`` which
        # json.loads rejects as "Extra data". Decode just the first complete
        # value from the expected opener and ignore whatever follows. Genuine
        # garbage (no opener, or an undecodable value) still raises loudly so the
        # retry-with-feedback layer fires.
        payload = first_json_value(text, expect)
    if expect == "object":
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
        obj = cast("dict[str, object]", payload)
        missing = [k for k in required_keys if k not in obj]
        if missing:
            raise ValueError(f"JSON object missing required key(s): {missing}")
        return obj
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON array, got {type(payload).__name__}")
    return cast("list[object]", payload)


def normalize_extracted_text(text: str) -> str:
    """Apply the shared nfkc-lines-v1 preflight and OCR text normalization."""
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def canonical_json(value: object) -> str:
    """Compact sorted JSON, ``ensure_ascii=False`` — the dominant stored-payload form."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json_ascii(value: object) -> str:
    """Compact sorted JSON with ``ensure_ascii=True`` (``\\uXXXX`` escapes)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_json_no_nan(value: object) -> str:
    """Compact sorted JSON, ``ensure_ascii=False``, rejecting NaN/Infinity."""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def canonical_json_ascii_no_nan(value: object) -> str:
    """Compact sorted JSON with ``ensure_ascii=True``, rejecting NaN/Infinity."""
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def canonical_json_ascii_default_str(value: object) -> str:
    """Compact sorted JSON, ``ensure_ascii=True``, coercing non-JSON types via ``str``."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_json_model(value: object) -> str:
    """``canonical_json`` after dumping a pydantic ``BaseModel`` to JSON-compatible values."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# SHA-256 digests — text, bytes, file, and separator-joined identity forms.
# --------------------------------------------------------------------------


def sha256_text(value: str) -> str:
    """SHA-256 hex digest of a text value's UTF-8 bytes."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file's bytes, streamed in 1 MiB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_joined_unit_sep(*parts: str) -> str:
    """SHA-256 over the ``\\x1f`` (unit separator) join of the parts — population record identity."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def sha256_joined_nul(*parts: str) -> str:
    """SHA-256 over the ``\\0`` (NUL) join of the parts — namespaced payload identity."""
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def content_sha256(value: object) -> str:
    """SHA-256 over ``canonical_json(value)`` — the stored content-hash form."""
    return sha256_text(canonical_json(value))


def content_sha256_ascii(value: object) -> str:
    """SHA-256 over ``canonical_json_ascii(value)``."""
    return sha256_text(canonical_json_ascii(value))


def content_sha256_no_nan(value: object) -> str:
    """SHA-256 over ``canonical_json_no_nan(value)``."""
    return sha256_text(canonical_json_no_nan(value))


def content_sha256_ascii_no_nan(value: object) -> str:
    """SHA-256 over ``canonical_json_ascii_no_nan(value)``."""
    return sha256_text(canonical_json_ascii_no_nan(value))


def content_sha256_model(value: object) -> str:
    """SHA-256 over ``canonical_json_model(value)``."""
    return sha256_text(canonical_json_model(value))


def content_sha256_ascii_default_str(value: object) -> str:
    """SHA-256 over ``canonical_json_ascii_default_str(value)``."""
    return sha256_text(canonical_json_ascii_default_str(value))


def content_sha256_or_text(value: object) -> str:
    """Hash a ``str`` as-is; anything else as ``canonical_json(value)``."""
    material = value if isinstance(value, str) else canonical_json(value)
    return sha256_text(material)


def content_sha256_ascii_or_text(value: object) -> str:
    """Hash a ``str`` as-is; anything else as ``canonical_json_ascii(value)``."""
    material = value if isinstance(value, str) else canonical_json_ascii(value)
    return sha256_text(material)


def content_sha256_model_or_text(value: object) -> str:
    """Hash a ``str`` as-is; anything else as ``canonical_json_model(value)``."""
    material = value if isinstance(value, str) else canonical_json_model(value)
    return sha256_text(material)


# --------------------------------------------------------------------------
# SHA-256 validators — normalize-or-reject stored digest columns.
# --------------------------------------------------------------------------


def require_sha256(value: str, *, message: str) -> str:
    """Require a lowercase 64-hex SHA-256, normalizing case; raise ``ValueError(message)`` otherwise."""
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(message)
    return normalized


def require_sha256_exact(value: str, *, message: str) -> str:
    """Require an already-lowercase 64-hex SHA-256 without normalizing case."""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(message)
    return value


# --------------------------------------------------------------------------
# UTC coercion and ISO-8601 parsing.
# --------------------------------------------------------------------------


def as_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC (naive values are assumed UTC)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_instant(value: object) -> datetime:
    """Parse a datetime-or-ISO-string to aware UTC (naive values are assumed UTC)."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return as_utc(parsed)


def parse_iso8601_utc(value: object) -> datetime:
    """Parse ``str(value)`` as ISO-8601 (``Z`` tolerated) and coerce to aware UTC.

    Datetime inputs are re-parsed through their string form — the legacy
    projection/publication clock semantics (fold-ambiguity is lost, matching the
    original implementations this replaced).
    """
    return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def parse_iso8601_cli(value: str) -> datetime:
    """CLI ``type=`` parser: ISO-8601 datetime with ``Z`` tolerated, no UTC coercion."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from error


def parse_iso8601_named(value: object, name: str) -> datetime:
    """Parse ISO-8601 (``Z`` tolerated) naming the field in ``RuntimeError`` messages."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError(f"{name} must be ISO-8601") from error
    raise RuntimeError(f"{name} must be a datetime")


def parse_date_text_or_none(raw: object) -> date | None:
    """Parse a text value to a ``date``; ``None`` for empty/non-text/unparseable input."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


# --------------------------------------------------------------------------
# sqlite_master table checks — two deliberate error policies.
# --------------------------------------------------------------------------


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True iff ``name`` is a table; sqlite errors propagate (fail closed)."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def table_exists_or_false(conn: sqlite3.Connection, name: str) -> bool:
    """True iff ``name`` is a table; any sqlite error reads as absence."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def table_or_view_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True iff ``name`` is a table or view; sqlite errors propagate."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
        is not None
    )

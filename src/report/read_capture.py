"""Report-only bounded SQL capture primitive (frozen query commitments + replay).

Tested query capture primitive for report publication evidence, deliberately
separate from public generation-context schemas: no general SQL framework,
copies no fact rows, opens no writer, makes no network or model calls.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from datetime import date, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dcf.input_evidence import canonical_digest
from sqlite_runtime import (
    COMMIT_PATTERN,
    ROLLBACK_PATTERN,
    ManagedConnection,
    ManagedCursor,
    SQLiteConnectionRole,
    connect_sqlite,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_PROGRESS_INTERVAL = 1_000
_ALLOWED_ROW_FACTORIES = (None, sqlite3.Row, tuple)
_ALLOWED_TEXT_FACTORIES = (str,)


_INTROSPECTION_PRAGMAS: frozenset[str] = frozenset(
    {"table_info", "index_list", "index_info", "index_xinfo", "foreign_key_list", "table_list"}
)
_VERSION_PRAGMAS: frozenset[str] = frozenset({"user_version", "application_id"})
_READ_DIAGNOSTIC_PRAGMAS: frozenset[str] = frozenset(
    {"database_list", "data_version", "schema_version", "journal_mode"}
)
_CONNECTION_DIAGNOSTIC_PRAGMAS: frozenset[str] = frozenset(
    {"busy_timeout", "foreign_keys", "query_only"}
)
_DIAGNOSTIC_PRAGMAS: frozenset[str] = _READ_DIAGNOSTIC_PRAGMAS | _CONNECTION_DIAGNOSTIC_PRAGMAS

_PRAGMA_READONLY_PATTERN = re.compile(
    r"^\s*(?:(?:/\*.*?\*/|--[^\n]*)\s*)*pragma\s+(?:[a-zA-Z0-9_]+\s*\.\s*)?([a-zA-Z0-9_]+)\s*;?\s*(?:(?:/\*.*?\*/|--[^\n]*)\s*)*$",
    re.IGNORECASE | re.DOTALL,
)
_HEX_LOWER_PATTERN = re.compile(r"^[0-9a-f]*$")
_SQLITE_LIMIT_ERROR_PATTERNS: tuple[str, ...] = (
    "string or blob too big",
    "too many columns",
    "too many sql variables",
    "expression tree is too large",
    "maximum depth",
)

_BASE_ALLOWED_FUNCTIONS: frozenset[str] = (
    frozenset({"abs", "avg", "char", "coalesce", "count", "date", "datetime"})
    | frozenset({"derive_retrieval_scope_id", "fact_sha256", "glob", "group_concat", "hex"})
    | frozenset({"iif", "ifnull", "instr", "julianday", "length", "like", "lower", "ltrim"})
    | frozenset({"json_array_length", "json_extract", "json_type", "json_valid"})
    | frozenset({"max", "min", "nullif", "replace", "round", "rtrim"})
    | frozenset({"strftime", "substr", "sum", "time", "total", "trim"})
    | frozenset({"typeof", "unicode", "upper"})
)


class ReadCaptureUnavailableError(RuntimeError):
    """Typed capture refusal. Never a partial receipt or a degraded result."""

    def __init__(self, reason_code: str, detail: str | None = None) -> None:
        extra = f" ({detail})" if detail is not None else ""
        super().__init__(f"report read capture unavailable: {reason_code}{extra}")
        self.reason_code = reason_code
        self.detail = detail


ReadCaptureUnavailable = ReadCaptureUnavailableError


def _validate_connection_factories(
    conn: sqlite3.Connection, cursor_row_factory: Any = None
) -> None:
    if conn.text_factory not in _ALLOWED_TEXT_FACTORIES:
        raise ReadCaptureUnavailable("unsupported_factory", f"text_factory={conn.text_factory!r}")
    if conn.row_factory not in _ALLOWED_ROW_FACTORIES:
        raise ReadCaptureUnavailable(
            "unsupported_factory", f"conn.row_factory={conn.row_factory!r}"
        )
    if cursor_row_factory not in _ALLOWED_ROW_FACTORIES:
        raise ReadCaptureUnavailable(
            "unsupported_factory", f"cursor.row_factory={cursor_row_factory!r}"
        )


def _extract_readonly_pragma_name(sql: str) -> str | None:
    match = _PRAGMA_READONLY_PATTERN.match(sql)
    name = match.group(1).casefold() if match is not None else None
    return name if name in _DIAGNOSTIC_PRAGMAS else None


def _translate_sqlite_limit_error(error: sqlite3.DatabaseError) -> ReadCaptureUnavailable | None:
    code = getattr(error, "sqlite_errorcode", None)
    if code == getattr(sqlite3, "SQLITE_TOOBIG", 18):
        return ReadCaptureUnavailable("budget_exhausted", str(error))
    msg = str(error).casefold()
    if any(pattern in msg for pattern in _SQLITE_LIMIT_ERROR_PATTERNS):
        return ReadCaptureUnavailable("budget_exhausted", str(error))
    return None


class CapturedValue(BaseModel):
    """One normalized SQLite binding or result scalar in typed replay form."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["null", "int", "float", "text", "blob"]
    value: int | float | str | None = None

    @model_validator(mode="after")
    def _check_kind(self) -> CapturedValue:
        k, v = self.kind, self.value
        if k == "null" and v is not None:
            raise ValueError("null value carries no payload")
        if k == "int" and (
            not isinstance(v, int) or isinstance(v, bool) or not -(2**63) <= v < 2**63
        ):
            raise ValueError("int value required in SQLite range")
        if k == "float" and (not isinstance(v, int | float) or isinstance(v, bool)):
            raise ValueError("float value required")
        if k == "text" and not isinstance(v, str):
            raise ValueError("text value required")
        if k == "blob":
            if not isinstance(v, str) or len(v) % 2 != 0:
                raise ValueError("blob value must be hex text")
            if not _HEX_LOWER_PATTERN.fullmatch(v):
                raise ValueError("blob value must be canonical lowercase hex")
        return self


class CapturedParameters(BaseModel):
    """Bindings normalized once before the one actual execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    style: Literal["positional", "named"]
    values: tuple[CapturedValue, ...]
    keys: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _check_style(self) -> CapturedParameters:
        if self.style == "named" and (self.keys is None or len(self.keys) != len(self.values)):
            raise ValueError("named parameters require matching keys and values")
        if self.style != "named" and self.keys is not None:
            raise ValueError("positional parameters carry no keys")
        return self


class CapturedStatement(BaseModel):
    """One frozen query commitment; no copied fact rows are persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    sql: str = Field(min_length=1)
    parameters: CapturedParameters
    columns: tuple[str, ...]
    column_types: tuple[str | None, ...]
    row_count: int = Field(ge=0)
    result_sha256: Sha256


class CaptureLimits(BaseModel):
    """Explicit per-report budgets with conservative defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_statements: int = Field(default=4096, gt=0)
    max_rows_per_query: int = Field(default=100_000, gt=0)
    max_total_rows: int = Field(default=1_000_000, gt=0)
    max_row_bytes_per_query: int = Field(default=32 * 1024 * 1024, gt=0)
    max_total_row_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    max_sql_bytes: int = Field(default=1024 * 1024, gt=0)
    max_parameters: int = Field(default=999, gt=0)
    max_parameter_bytes: int = Field(default=1024 * 1024, gt=0)
    max_columns: int = Field(default=512, gt=0)
    max_expression_depth: int = Field(default=64, gt=0)
    max_instructions: int = Field(default=50_000_000, gt=0)
    execution_deadline_s: float = Field(default=60.0, gt=0)


class CaptureReceipt(BaseModel):
    """Sealed capture receipt: commitments and replay parameters only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["report_read_capture.v1"] = "report_read_capture.v1"
    schema_sha256: Sha256
    statements: tuple[CapturedStatement, ...]
    diagnostics: tuple[str, ...] = ()
    total_rows: int = Field(default=0, ge=0)
    total_row_bytes: int = Field(default=0, ge=0)
    total_instructions: int = Field(default=0, ge=0)

    @property
    def semantic_sha256(self) -> str:
        stmts = [
            {
                "sql": s.sql,
                "parameters": s.parameters.model_dump(mode="json"),
                "columns": list(s.columns),
                "column_types": list(s.column_types),
                "row_count": s.row_count,
                "result_sha256": s.result_sha256,
            }
            for s in self.statements
        ]
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "schema_sha256": self.schema_sha256,
                "statements": stmts,
            }
        )


def _raw_parameter_size(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool | int | float):
        return 8
    if isinstance(value, str):
        try:
            return len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ReadCaptureUnavailable("unsupported_parameter_type", "text not UTF-8") from error
    if isinstance(value, bytes | bytearray):
        return len(value)
    if isinstance(value, memoryview):
        return value.nbytes
    if isinstance(value, datetime):
        return len(value.isoformat(" ").encode("utf-8"))
    if isinstance(value, date):
        return len(value.isoformat().encode("utf-8"))
    raise ReadCaptureUnavailable("unsupported_parameter_type", type(value).__name__)


def _normalize_parameter(value: object) -> CapturedValue:
    if value is None:
        return CapturedValue(kind="null")
    if isinstance(value, bool):
        return CapturedValue(kind="int", value=int(value))
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ReadCaptureUnavailable("unsupported_parameter_type", "integer out of range")
        return CapturedValue(kind="int", value=value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReadCaptureUnavailable("unsupported_parameter_type", "non-finite float")
        return CapturedValue(kind="float", value=value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ReadCaptureUnavailable("unsupported_parameter_type", "text not UTF-8") from error
        return CapturedValue(kind="text", value=value)
    if isinstance(value, bytes | bytearray | memoryview):
        val_hex = value.hex() if isinstance(value, bytes | bytearray) else value.tobytes().hex()
        return CapturedValue(kind="blob", value=val_hex)
    if isinstance(value, datetime):
        return CapturedValue(kind="text", value=value.isoformat(" "))
    if isinstance(value, date):
        return CapturedValue(kind="text", value=value.isoformat())
    raise ReadCaptureUnavailable("unsupported_parameter_type", type(value).__name__)


def _normalized_python_value(value: CapturedValue) -> object:
    k, v = value.kind, value.value
    if k == "null":
        return None
    if k == "int" and isinstance(v, int) and not isinstance(v, bool):
        return v
    if k == "float" and isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    if k == "text" and isinstance(v, str):
        return v
    if k == "blob" and isinstance(v, str) and len(v) % 2 == 0:
        return bytes.fromhex(v)
    raise ReadCaptureUnavailable("invalid_parameter_encoding", k)


def _encode_result_value(value: object) -> dict[str, object]:
    if value is None:
        return {"t": "null", "v": None}
    if isinstance(value, bool | int):
        return {"t": "int", "v": int(value)}
    if isinstance(value, float):
        return {"t": "float", "v": value if math.isfinite(value) else repr(value)}
    if isinstance(value, str):
        return {"t": "text", "v": value}
    if isinstance(value, bytes | bytearray | memoryview):
        hex_val = value.hex() if isinstance(value, bytes | bytearray) else value.tobytes().hex()
        return {"t": "blob", "v": hex_val}
    raise ReadCaptureUnavailable("unsupported_result_value", type(value).__name__)


def _row_size(row: Any) -> int:
    return sum(
        len(v.encode("utf-8"))
        if isinstance(v, str)
        else len(v)
        if isinstance(v, bytes | bytearray)
        else v.nbytes
        if isinstance(v, memoryview)
        else 0
        if v is None
        else 8
        for v in row
    )


def _parameter_size(value: CapturedValue) -> int:
    k, v = value.kind, value.value
    if k == "null":
        return 0
    if k in ("int", "float"):
        return 8
    if k == "text" and isinstance(v, str):
        return len(v.encode("utf-8"))
    if k == "blob" and isinstance(v, str) and len(v) % 2 == 0:
        return len(v) // 2
    raise ReadCaptureUnavailable("invalid_parameter_encoding", k)


def _result_digest(
    sql: str,
    parameters: CapturedParameters,
    columns: tuple[str, ...],
    column_types: tuple[str | None, ...],
    rows: list[Any],
) -> str:
    encoded_rows = [[_encode_result_value(row[idx]) for idx in range(len(row))] for row in rows]
    return canonical_digest(
        {
            "sql": sql,
            "parameters": parameters.model_dump(mode="json"),
            "columns": list(columns),
            "column_types": list(column_types),
            "rows": encoded_rows,
        }
    )


def _schema_digest(conn: sqlite3.Connection, limits: CaptureLimits) -> str:
    cursor = sqlite3.Connection.execute(
        conn, "SELECT type, name, tbl_name, sql FROM main.sqlite_master ORDER BY type, name"
    )
    rows: list[Any] = []
    total_bytes = 0
    max_rows = max(limits.max_rows_per_query, limits.max_total_rows)
    max_bytes = max(limits.max_row_bytes_per_query, limits.max_total_row_bytes)
    while True:
        row = sqlite3.Cursor.fetchone(cursor)
        if row is None:
            break
        row_bytes = _row_size(row)
        if len(rows) >= max_rows:
            raise ReadCaptureUnavailable("budget_exhausted", "schema row limit exceeded")
        if total_bytes + row_bytes > max_bytes:
            raise ReadCaptureUnavailable("budget_exhausted", "schema byte limit exceeded")
        rows.append(row)
        total_bytes += row_bytes
    encoded = [[_encode_result_value(row[idx]) for idx in range(len(row))] for row in rows]
    return canonical_digest({"schema": "main.sqlite_master", "rows": encoded})


def _description_metadata(cursor: sqlite3.Cursor) -> tuple[tuple[str, ...], tuple[str | None, ...]]:
    desc = cursor.description
    if desc is None:
        return (), ()
    return tuple(str(c[0]) for c in desc), tuple(
        str(c[1]) if c[1] is not None else None for c in desc
    )


def _apply_connection_local_sqlite_limits(
    conn: sqlite3.Connection, limits: CaptureLimits, *, schema_phase: bool = False
) -> None:
    max_len = (
        max(limits.max_row_bytes_per_query, 1024 * 1024)
        if schema_phase
        else limits.max_row_bytes_per_query
    )
    max_sql = max(limits.max_sql_bytes, 1024 * 1024) if schema_phase else limits.max_sql_bytes
    max_col = max(limits.max_columns, 5) if schema_phase else limits.max_columns
    cats = (
        (sqlite3.SQLITE_LIMIT_LENGTH, max_len),
        (sqlite3.SQLITE_LIMIT_SQL_LENGTH, max_sql),
        (sqlite3.SQLITE_LIMIT_COLUMN, max_col),
        (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, limits.max_expression_depth),
        (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limits.max_parameters),
        (sqlite3.SQLITE_LIMIT_ATTACHED, 0),
    )
    for cat, req in cats:
        try:
            conn.setlimit(cat, req)
            obs = conn.getlimit(cat)
        except (AttributeError, sqlite3.Error) as err:
            raise ReadCaptureUnavailable(
                "limit_enforcement_unavailable", f"cat={cat}: {err}"
            ) from err
        if obs > req:
            raise ReadCaptureUnavailable(
                "limit_enforcement_unavailable", f"cat={cat}: obs={obs} req={req}"
            )


class _SandboxState:
    """Budgets + explicit SQL sandbox callbacks for one report boundary."""

    def __init__(self, *, limits: CaptureLimits, allowed_functions: frozenset[str]) -> None:
        self.limits = limits
        self.allowed_functions = _BASE_ALLOWED_FUNCTIONS | allowed_functions
        self.total_statements: int = 0
        self.total_rows: int = 0
        self.total_row_bytes: int = 0
        self.total_instructions: int = 0
        self.deadline = 0.0
        self.current_pragma: str | None = None
        self.denial: ReadCaptureUnavailable | None = None
        self.allow_transaction_control: bool = False

    def pop_denial(self) -> ReadCaptureUnavailable | None:
        denial = self.denial
        self.denial = None
        return denial

    def prepare_execution(self, sql: str, parameters: object) -> tuple[CapturedParameters, Any]:
        self._check_deadline()
        if self.total_statements >= self.limits.max_statements:
            raise ReadCaptureUnavailable("statement_limit_exceeded")
        try:
            if len(sql.encode("utf-8")) > self.limits.max_sql_bytes:
                raise ReadCaptureUnavailable("sql_length_limit_exceeded")
        except UnicodeEncodeError as error:
            raise ReadCaptureUnavailable("unsupported_sql_encoding") from error

        values: list[CapturedValue] = []
        if parameters is None or (isinstance(parameters, tuple | list) and not parameters):
            keys, style = None, "positional"
        elif isinstance(parameters, dict):
            named = cast("dict[str, object]", parameters)
            if len(named) > self.limits.max_parameters:
                raise ReadCaptureUnavailable("parameter_count_limit_exceeded")
            encoded_bytes = 0
            keys = tuple(named)
            style = "named"
            for k in keys:
                encoded_bytes += len(k.encode("utf-8"))
                val = named[k]
                encoded_bytes += _raw_parameter_size(val)
                if encoded_bytes > self.limits.max_parameter_bytes:
                    raise ReadCaptureUnavailable("parameter_bytes_limit_exceeded")
                values.append(_normalize_parameter(val))
        elif isinstance(parameters, tuple | list):
            positional = cast("Sequence[object]", parameters)
            if len(positional) > self.limits.max_parameters:
                raise ReadCaptureUnavailable("parameter_count_limit_exceeded")
            encoded_bytes = 0
            keys, style = None, "positional"
            for item in positional:
                encoded_bytes += _raw_parameter_size(item)
                if encoded_bytes > self.limits.max_parameter_bytes:
                    raise ReadCaptureUnavailable("parameter_bytes_limit_exceeded")
                values.append(_normalize_parameter(item))
        else:
            raise ReadCaptureUnavailable(
                "unrecordable_execution_path", f"parameters={type(parameters).__name__}"
            )

        captured = CapturedParameters(style=style, values=tuple(values), keys=keys)
        execution: object = (
            {k: _normalized_python_value(v) for k, v in zip(keys or (), values, strict=True)}
            if style == "named"
            else tuple(_normalized_python_value(val) for val in values)
        )
        return captured, execution

    def _check_deadline(self) -> None:
        if self.deadline > 0.0 and time.monotonic() > self.deadline:
            raise ReadCaptureUnavailable("execution_deadline_exceeded")

    def authorize(
        self, action: int, arg1: str | None, arg2: str | None, db: str | None, trg: str | None
    ) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_TRANSACTION, getattr(sqlite3, "SQLITE_SAVEPOINT", 32)):
            if self.allow_transaction_control:
                return sqlite3.SQLITE_OK
            self.denial = ReadCaptureUnavailable("transaction_control_forbidden", arg1)
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            name = (arg2 or arg1 or "").casefold()
            if name in self.allowed_functions:
                return sqlite3.SQLITE_OK
            self.denial = ReadCaptureUnavailable("sandbox_denied_function", name)
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            name = (arg1 or "").casefold().split(".")[-1]
            if name in _INTROSPECTION_PRAGMAS:
                return sqlite3.SQLITE_OK
            if arg2 is not None:
                self.denial = ReadCaptureUnavailable("sandbox_denied_pragma_write", name)
            elif name in _DIAGNOSTIC_PRAGMAS or name in _VERSION_PRAGMAS:
                self.current_pragma = name if name in _DIAGNOSTIC_PRAGMAS else None
                return sqlite3.SQLITE_OK
            else:
                self.denial = ReadCaptureUnavailable("sandbox_denied_pragma", name)
            return sqlite3.SQLITE_DENY
        self.denial = ReadCaptureUnavailable("sandbox_denied_action", f"action={action}")
        return sqlite3.SQLITE_DENY

    def progress(self) -> int:
        self.total_instructions += _PROGRESS_INTERVAL
        if self.total_instructions > self.limits.max_instructions:
            self.denial = ReadCaptureUnavailable("instruction_budget_exhausted")
            return 1
        if self.deadline > 0.0 and time.monotonic() > self.deadline:
            self.denial = ReadCaptureUnavailable("execution_deadline_exceeded")
            return 1
        return 0


class _RecorderState(_SandboxState):
    """One capture's recording state: commitments, diagnostics, invalidation."""

    def __init__(self, *, limits: CaptureLimits, allowed_functions: frozenset[str]) -> None:
        super().__init__(limits=limits, allowed_functions=allowed_functions)
        self.capturing = False
        self.invalid_reason: str | None = None
        self.statements: list[CapturedStatement] = []
        self.diagnostics: list[str] = []
        self.classified_pragmas: dict[str, str] = {}
        self.schema_sha256 = ""

    def invalidate(self, reason: str) -> None:
        if self.invalid_reason is None:
            self.invalid_reason = reason
        self.capturing = False

    def record_execution(
        self,
        cursor: sqlite3.Cursor,
        sql: str,
        parameters: CapturedParameters,
        rows: list[Any],
        row_bytes: int,
    ) -> None:
        columns, column_types = _description_metadata(cursor)
        self.total_statements += 1
        self.total_rows += len(rows)
        self.total_row_bytes += row_bytes
        pragma_name = (
            self.current_pragma
            or self.classified_pragmas.get(sql)
            or _extract_readonly_pragma_name(sql)
        )
        self.current_pragma = None
        if pragma_name in _DIAGNOSTIC_PRAGMAS:
            self.classified_pragmas[sql] = pragma_name
            self.diagnostics.append(sql)
            return
        digest = _result_digest(sql, parameters, columns, column_types, rows)
        self.statements.append(
            CapturedStatement(
                sql=sql,
                parameters=parameters,
                columns=columns,
                column_types=column_types,
                row_count=len(rows),
                result_sha256=digest,
            )
        )


_CaptureCoordinator = _RecorderState


def _prefetch_complete_result(
    cursor: sqlite3.Cursor,
    sandbox: _SandboxState,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[Any], int]:
    if cursor.description is None:
        return [], 0
    limits = sandbox.limits
    rows: list[Any] = []
    row_bytes = 0
    saved_cursor_factory = cursor.row_factory
    saved_conn_factory = conn.row_factory if conn is not None else None
    cursor.row_factory = None
    if conn is not None:
        conn.row_factory = None
    try:
        while True:
            row = sqlite3.Cursor.fetchone(cursor)
            if row is None:
                break
            size = _row_size(row)
            if len(rows) >= limits.max_rows_per_query:
                raise ReadCaptureUnavailable("query_row_limit_exceeded")
            if row_bytes + size > limits.max_row_bytes_per_query:
                raise ReadCaptureUnavailable("query_byte_limit_exceeded")
            if sandbox.total_rows + len(rows) + 1 > limits.max_total_rows:
                raise ReadCaptureUnavailable("total_row_limit_exceeded")
            if sandbox.total_row_bytes + row_bytes + size > limits.max_total_row_bytes:
                raise ReadCaptureUnavailable("total_byte_limit_exceeded")
            rows.append(row)
            row_bytes += size
    finally:
        cursor.row_factory = saved_cursor_factory
        if conn is not None:
            conn.row_factory = saved_conn_factory
    return rows, row_bytes


class RecordingCursor(ManagedCursor):
    """Cursor that serves the parser from the complete bounded prefetch."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._capture_rows: deque[tuple[Any, ...]] = deque()
        self._capture_buffered = False

    def _recorder(self) -> _RecorderState | None:
        conn = getattr(self, "connection", None)
        if isinstance(conn, RecordingManagedConnection):
            rec = getattr(conn, "capture_recorder", None)
            if isinstance(rec, _RecorderState):
                return rec
        return None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "row_factory" and value not in _ALLOWED_ROW_FACTORIES:
            rec = self._recorder()
            if rec is not None and rec.capturing:
                rec.invalidate("unsupported_factory")
            raise ReadCaptureUnavailable("unsupported_factory", f"row_factory={value!r}")
        super().__setattr__(name, value)

    def _transform_row(self, raw_row: tuple[Any, ...]) -> Any:
        conn = self.connection
        _validate_connection_factories(conn, self.row_factory)
        factory = self.row_factory
        if factory is None or factory is tuple:
            return raw_row
        if factory is sqlite3.Row:
            return sqlite3.Row(self, raw_row)
        raise ReadCaptureUnavailable("unsupported_factory", f"factory={factory!r}")

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        recorder = self._recorder()
        if recorder is None or not recorder.capturing:
            self._capture_rows.clear()
            self._capture_buffered = False
            return super().execute(*args, **kwargs)
        conn = self.connection
        _validate_connection_factories(conn, self.row_factory)
        if not conn.in_transaction:
            recorder.invalidate("snapshot_lost")
            raise ReadCaptureUnavailable(
                "snapshot_lost", "SQLite read transaction snapshot is no longer active"
            )
        sql = args[0] if args else kwargs.get("sql") or kwargs.get("operation")
        parameters = args[1] if len(args) > 1 else kwargs.get("parameters")
        if not isinstance(sql, str):
            raise ReadCaptureUnavailable("unrecordable_execution_path", f"sql={type(sql).__name__}")
        if kwargs and set(kwargs) - {"sql", "operation", "parameters"}:
            raise ReadCaptureUnavailable(
                "unrecordable_execution_path", "unsupported execute kwargs"
            )
        recorder.current_pragma = None
        if COMMIT_PATTERN.match(sql) or ROLLBACK_PATTERN.match(sql):
            recorder.invalidate("transaction_boundary")
            raise ReadCaptureUnavailable("transaction_boundary_during_capture", sql[:64])
        self._capture_rows.clear()
        self._capture_buffered = False
        try:
            captured, execution = recorder.prepare_execution(sql, parameters)
            super().execute(sql, execution)
            rows, row_bytes = _prefetch_complete_result(self, recorder, conn)
            recorder.record_execution(self, sql, captured, rows, row_bytes)
        except ReadCaptureUnavailable as refusal:
            recorder.invalidate(refusal.reason_code)
            raise
        except sqlite3.DatabaseError as error:
            denial = recorder.pop_denial() or _translate_sqlite_limit_error(error)
            recorder.invalidate(denial.reason_code if denial is not None else "execution_failure")
            raise (denial if denial is not None else error) from error
        except BaseException:
            recorder.invalidate("execution_failure")
            raise
        self._capture_buffered = True
        self._capture_rows.extend(rows)
        return self

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        recorder = self._recorder()
        if recorder is not None and recorder.capturing:
            conn = self.connection
            if not conn.in_transaction:
                recorder.invalidate("snapshot_lost")
                raise ReadCaptureUnavailable(
                    "snapshot_lost", "SQLite read transaction snapshot is no longer active"
                )
            recorder.invalidate("unrecordable_execution_path")
            raise ReadCaptureUnavailable("unrecordable_execution_path", "executemany")
        return super().executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        recorder = self._recorder()
        if recorder is not None and recorder.capturing:
            recorder.invalidate("unrecordable_execution_path")
            raise ReadCaptureUnavailable("unrecordable_execution_path", "executescript")
        return super().executescript(*args, **kwargs)

    def fetchone(self) -> Any:
        if not self._capture_buffered:
            return super().fetchone()
        _validate_connection_factories(self.connection, self.row_factory)
        return self._transform_row(self._capture_rows.popleft()) if self._capture_rows else None

    def fetchmany(self, size: int | None = None) -> list[Any]:
        if not self._capture_buffered:
            return super().fetchmany() if size is None else super().fetchmany(size)
        _validate_connection_factories(self.connection, self.row_factory)
        wanted = self.arraysize if size is None else size
        count = min(max(wanted, 0), len(self._capture_rows))
        return [self._transform_row(self._capture_rows.popleft()) for _ in range(count)]

    def fetchall(self) -> list[Any]:
        if not self._capture_buffered:
            return super().fetchall()
        _validate_connection_factories(self.connection, self.row_factory)
        taken = [self._transform_row(row) for row in self._capture_rows]
        self._capture_rows.clear()
        return taken

    def __next__(self) -> Any:
        if not self._capture_buffered:
            return super().__next__()
        _validate_connection_factories(self.connection, self.row_factory)
        if self._capture_rows:
            return self._transform_row(self._capture_rows.popleft())
        raise StopIteration

    def close(self) -> None:
        self._capture_rows.clear()
        self._capture_buffered = False
        super().close()


class RecordingManagedConnection(ManagedConnection):
    """Read-only connection hosting exactly one bounded capture recorder."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.capture_recorder: _RecorderState | None = None
        self.capture_authorizer_owner: str | None = None
        self.capture_progress_owner: str | None = None

    def _recorder(self) -> _RecorderState | None:
        rec = getattr(self, "capture_recorder", None)
        return rec if isinstance(rec, _RecorderState) else None

    @property
    def protected_read_snapshot_active(self) -> bool:
        """True when the connection is held under an immutable read snapshot owner."""
        rec = self._recorder()
        return rec is not None and rec.capturing and self.in_transaction

    def __setattr__(self, name: str, value: Any) -> None:
        rec = self._recorder()
        if (name == "row_factory" and value not in _ALLOWED_ROW_FACTORIES) or (
            name == "text_factory" and value not in _ALLOWED_TEXT_FACTORIES
        ):
            if rec is not None and rec.capturing:
                rec.invalidate("unsupported_factory")
            raise ReadCaptureUnavailable("unsupported_factory", f"{name}={value!r}")
        if name in ("autocommit", "isolation_level") and rec is not None and rec.capturing:
            rec.invalidate("connection_transaction_mode_changed")
            raise ReadCaptureUnavailable(
                "transaction_boundary_violated",
                "Cannot mutate transaction mode during read capture",
            )
        super().__setattr__(name, value)

    def _guard_callback_owner(
        self, kind: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        rec = self._recorder()
        if rec is not None and rec.capturing:
            raise ReadCaptureUnavailable("capture_callback_conflict", kind)
        if args:
            cb: object = args[0]
        elif kind == "authorizer":
            cb = kwargs.get("authorizer_callback", kwargs.get("callback"))
        else:
            fallback = next((v for k, v in kwargs.items() if k != "n"), None)
            cb = kwargs.get("progress_handler", kwargs.get("callback", fallback))
        is_auth = kind == "authorizer"
        current = self.capture_authorizer_owner if is_auth else self.capture_progress_owner
        if callable(cb):
            if current is not None:
                raise ReadCaptureUnavailable("capture_callback_conflict", kind)
            val: str | None = "user"
        elif cb is None and current == "user":
            val = None
        else:
            return
        if is_auth:
            self.capture_authorizer_owner = val
        else:
            self.capture_progress_owner = val

    def set_authorizer(self, *args: Any, **kwargs: Any) -> None:
        self._guard_callback_owner("authorizer", args, kwargs)
        sqlite3.Connection.set_authorizer(self, *args, **kwargs)

    def set_progress_handler(self, *args: Any, **kwargs: Any) -> None:
        self._guard_callback_owner("progress_handler", args, kwargs)
        sqlite3.Connection.set_progress_handler(self, *args, **kwargs)

    def cursor(self, factory: Any = RecordingCursor) -> sqlite3.Cursor:
        if factory is not RecordingCursor:
            raise ReadCaptureUnavailable("unsupported_cursor_factory", repr(factory))
        return super().cursor(factory)

    def _guard_transaction_boundary(self, op: str) -> None:
        rec = self._recorder()
        if rec is not None and rec.capturing:
            rec.invalidate("transaction_boundary")
            raise ReadCaptureUnavailable("transaction_boundary_during_capture", op)

    def commit(self) -> None:
        self._guard_transaction_boundary("commit")
        super().commit()

    def rollback(self) -> None:
        self._guard_transaction_boundary("rollback")
        super().rollback()

    def __enter__(self) -> RecordingManagedConnection:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> Literal[False]:
        self.commit() if exc_type is None else self.rollback()
        return False


class ReadCaptureSession:
    """One owner-controlled read transaction on one bounded recording connection."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        limits: CaptureLimits | None = None,
        allowed_functions: frozenset[str] | None = None,
        schema_preflight: bool | None = None,
    ) -> None:
        self.path = path
        self.limits = limits if limits is not None else CaptureLimits()
        self.allowed_functions = frozenset(allowed_functions or ())
        self.schema_preflight = schema_preflight
        self._conn: RecordingManagedConnection | None = None
        self._recorder: _RecorderState | None = None
        self._finished = False

    def open(self) -> RecordingManagedConnection:
        if self._conn is not None:
            raise ReadCaptureUnavailable("session_already_open")
        conn = connect_sqlite(
            self.path,
            role=SQLiteConnectionRole.READ_ONLY,
            schema_preflight=self.schema_preflight,
            connection_factory=RecordingManagedConnection,
        )
        if not isinstance(conn, RecordingManagedConnection):
            conn.close()
            raise ReadCaptureUnavailable("session_connection_factory_refused")
        self._conn = conn
        return conn

    def begin_capture(self) -> RecordingManagedConnection:
        conn = self._conn
        if conn is None or self._recorder is not None:
            raise ReadCaptureUnavailable(
                "session_not_open" if conn is None else "capture_already_attached"
            )
        if conn.capture_authorizer_owner is not None or conn.capture_progress_owner is not None:
            raise ReadCaptureUnavailable("capture_callback_conflict", "user callbacks present")
        _validate_connection_factories(conn)
        recorder = _RecorderState(limits=self.limits, allowed_functions=self.allowed_functions)
        conn.capture_recorder = recorder
        conn.capture_authorizer_owner = conn.capture_progress_owner = "recorder"
        try:
            sqlite3.Connection.set_authorizer(conn, recorder.authorize)
            sqlite3.Connection.set_progress_handler(conn, recorder.progress, _PROGRESS_INTERVAL)
            recorder.deadline = time.monotonic() + recorder.limits.execution_deadline_s
            recorder.allow_transaction_control = True
            try:
                sqlite3.Connection.execute(conn, "BEGIN")
            finally:
                recorder.allow_transaction_control = False
            _apply_connection_local_sqlite_limits(conn, recorder.limits, schema_phase=True)
            recorder.schema_sha256 = _schema_digest(conn, recorder.limits)
            _apply_connection_local_sqlite_limits(conn, recorder.limits, schema_phase=False)
            recorder.capturing = True
        except BaseException:
            recorder.invalidate("setup_failure")
            conn.capture_recorder = None
            conn.capture_authorizer_owner = None
            conn.capture_progress_owner = None
            raise
        self._recorder = recorder
        return conn

    def finish(self) -> CaptureReceipt:
        rec, conn = self._recorder, self._conn
        if rec is None or conn is None:
            raise ReadCaptureUnavailable("session_not_capturing")
        if rec.invalid_reason is not None or not rec.capturing:
            self._teardown()
            raise ReadCaptureUnavailable("capture_invalidated", rec.invalid_reason or "inactive")
        if not conn.in_transaction:
            rec.invalidate("snapshot_lost")
            self._teardown()
            raise ReadCaptureUnavailable(
                "snapshot_lost", "SQLite read transaction snapshot is no longer active"
            )
        rec.capturing = False
        receipt = CaptureReceipt(
            schema_sha256=rec.schema_sha256,
            statements=tuple(rec.statements),
            diagnostics=tuple(rec.diagnostics),
            total_rows=rec.total_rows,
            total_row_bytes=rec.total_row_bytes,
            total_instructions=rec.total_instructions,
        )
        rec.allow_transaction_control = True
        try:
            sqlite3.Connection.rollback(conn)
        finally:
            rec.allow_transaction_control = False
            rec.invalidate("finished")
            if not (
                getattr(conn, "_user_trace_callback", None) is not None
                or getattr(conn, "_trace_observers", None)
            ):
                conn.notify_rollback()
        self._finished = True
        self._teardown()
        return receipt

    def abort(self) -> None:
        rec, conn = self._recorder, self._conn
        if rec is not None:
            rec.invalidate("aborted")
            rec.allow_transaction_control = True
        if conn is not None and conn.in_transaction:
            has_trace = bool(
                getattr(conn, "_user_trace_callback", None) is not None
                or getattr(conn, "_trace_observers", None)
            )
            with suppress(sqlite3.Error):
                sqlite3.Connection.rollback(conn)
            if not has_trace:
                conn.notify_rollback()
        if rec is not None:
            rec.allow_transaction_control = False
        self._teardown()

    def _teardown(self) -> None:
        conn = self._conn
        self._recorder = None
        self._conn = None
        if conn is not None:
            with suppress(sqlite3.Error):
                conn.close()

    def __enter__(self) -> ReadCaptureSession:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.abort()


ReportReadCaptureSession = ReadCaptureSession


def replay_read_capture(
    path: str | os.PathLike[str],
    receipt: CaptureReceipt,
    *,
    limits: CaptureLimits,
    allowed_functions: frozenset[str] | None = None,
    schema_preflight: bool | None = None,
) -> CaptureReceipt:
    if receipt.schema_version != "report_read_capture.v1":
        raise ReadCaptureUnavailable("unsupported_receipt_version", receipt.schema_version)
    if len(receipt.statements) > limits.max_statements:
        raise ReadCaptureUnavailable("budget_exhausted", "statement count limit exceeded")
    conn = connect_sqlite(
        path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=schema_preflight
    )
    try:
        _validate_connection_factories(conn)
        state = _SandboxState(limits=limits, allowed_functions=frozenset(allowed_functions or ()))
        conn.set_authorizer(state.authorize)
        conn.set_progress_handler(state.progress, _PROGRESS_INTERVAL)
        state.deadline = time.monotonic() + limits.execution_deadline_s
        state.allow_transaction_control = True
        try:
            sqlite3.Connection.execute(conn, "BEGIN")
        finally:
            state.allow_transaction_control = False
        _apply_connection_local_sqlite_limits(conn, limits, schema_phase=True)
        if _schema_digest(conn, limits) != receipt.schema_sha256:
            raise ReadCaptureUnavailable("replay_schema_mismatch")
        _apply_connection_local_sqlite_limits(conn, limits, schema_phase=False)
        replayed: list[CapturedStatement] = []
        for c in receipt.statements:
            state.current_pragma = None
            if len(c.sql.encode("utf-8")) > limits.max_sql_bytes:
                raise ReadCaptureUnavailable("budget_exhausted", "sql length limit exceeded")
            if len(c.parameters.values) > limits.max_parameters:
                raise ReadCaptureUnavailable("budget_exhausted", "parameter count limit exceeded")
            param_bytes = sum(_parameter_size(val) for val in c.parameters.values)
            if c.parameters.keys is not None:
                param_bytes += sum(len(k.encode("utf-8")) for k in c.parameters.keys)
            if param_bytes > limits.max_parameter_bytes:
                raise ReadCaptureUnavailable("budget_exhausted", "parameter bytes limit exceeded")

            raw: object = (
                {
                    k: _normalized_python_value(v)
                    for k, v in zip(c.parameters.keys or (), c.parameters.values, strict=True)
                }
                if c.parameters.style == "named"
                else tuple(_normalized_python_value(v) for v in c.parameters.values)
            )
            captured, execution = state.prepare_execution(c.sql, raw)
            try:
                cursor = conn.execute(c.sql, execution)
                rows, row_bytes = _prefetch_complete_result(cursor, state, conn)
            except sqlite3.DatabaseError as error:
                denial = state.pop_denial() or _translate_sqlite_limit_error(error)
                if denial is not None:
                    raise denial from error
                raise
            cols, col_types = _description_metadata(cursor)
            state.total_statements += 1
            state.total_rows += len(rows)
            state.total_row_bytes += row_bytes
            digest = _result_digest(c.sql, captured, cols, col_types, rows)
            mismatch = (
                cols != c.columns
                or col_types != c.column_types
                or len(rows) != c.row_count
                or digest != c.result_sha256
            )
            if mismatch:
                raise ReadCaptureUnavailable("replay_result_mismatch", c.sql[:64])
            replayed.append(
                CapturedStatement(
                    sql=c.sql,
                    parameters=c.parameters,
                    columns=cols,
                    column_types=col_types,
                    row_count=len(rows),
                    result_sha256=digest,
                )
            )
        state.allow_transaction_control = True
        try:
            sqlite3.Connection.rollback(conn)
        finally:
            state.allow_transaction_control = False
        return CaptureReceipt(
            schema_sha256=receipt.schema_sha256,
            statements=tuple(replayed),
            diagnostics=tuple(receipt.diagnostics),
            total_rows=state.total_rows,
            total_row_bytes=state.total_row_bytes,
            total_instructions=state.total_instructions,
        )
    finally:
        conn.close()


__all__ = [
    "CaptureLimits",
    "CaptureReceipt",
    "CapturedParameters",
    "CapturedStatement",
    "CapturedValue",
    "ReadCaptureSession",
    "ReadCaptureUnavailable",
    "ReadCaptureUnavailableError",
    "RecordingCursor",
    "RecordingManagedConnection",
    "ReportReadCaptureSession",
    "replay_read_capture",
]

"""Global DCF macro assumptions — one editable source of truth.

The macro inputs that should be the SAME across every DCF model — the
risk-free rate, the equity/market risk premium, and a default tax rate — used
to live as hardcoded literals scattered through each builder. This module is
the single global default, backed by the ``global_dcf_assumptions`` table
(migration 0112). The dashboard panel writes it; the builders read it.

Precedence is **per-ticker override > global default > hardcoded fallback**:

    resolve("risk_free_rate", ticker_value=opus.get("risk_free_rate"),
            fallback=0.043)

returns the ticker's own value when it set one, else the global default from
the table, else the supplied fallback (the model's historical literal), else
the in-code seed default. So a hand-tuned per-name assumption still wins; the
global only governs names that don't override.

Every reader degrades gracefully: a missing DB / table (fresh checkout, test
fixture) falls back to ``SEED_DEFAULTS`` so a DCF build never fails because the
global store couldn't be read — mirroring the best-effort contract in
``src/llm_budget.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from db_paths import resolve_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

# The fields this store governs. Adding a field means a follow-up migration seed
# + a new attribute on GlobalDcfAssumptions; the setter rejects anything else.
KNOWN_FIELDS: frozenset[str] = frozenset({"risk_free_rate", "equity_risk_premium", "tax_rate"})

# In-code fallback, equal to migration 0112's seed (and the current FCFF
# literals). Used when the DB / table is unavailable so readers never crash.
SEED_DEFAULTS: dict[str, float] = {
    "risk_free_rate": 0.043,
    "equity_risk_premium": 0.045,
    "tax_rate": 0.24,
}

# Sane bounds for a decimal ratio dial (0.043 = 4.3%). The setter enforces these
# so a fat-fingered "4.3" (= 430%) from the panel is rejected, not persisted.
_MIN_VALUE = 0.0
_MAX_VALUE = 1.0


@dataclass(frozen=True, slots=True)
class GlobalDcfAssumptions:
    """Effective global macro inputs — every field resolved (stored value or
    seed default), never None. Builders read ``g.risk_free_rate`` etc. directly
    and keep their per-ticker override as the winning layer via
    ``opus.get("risk_free_rate", g.risk_free_rate)``."""

    risk_free_rate: float = SEED_DEFAULTS["risk_free_rate"]
    equity_risk_premium: float = SEED_DEFAULTS["equity_risk_premium"]
    tax_rate: float = SEED_DEFAULTS["tax_rate"]

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GlobalDcfAssumptionsLoad:
    """One atomic effective load plus the rows/version that produced it."""

    assumptions: GlobalDcfAssumptions
    source_record: dict[str, object]


StoredReadStatus = Literal[
    "ok",
    "missing_database",
    "missing_schema",
    "invalid_schema",
    "read_failed",
]


@dataclass(frozen=True, slots=True)
class _StoredRowsRead:
    rows: dict[str, tuple[float, str | None]]
    status: StoredReadStatus
    detail: str | None = None


def _read_stored_rows(db_path: Path | str | None) -> _StoredRowsRead:
    """Return exact stored values and their version timestamps."""
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return _StoredRowsRead({}, "missing_database")
    try:
        conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(global_dcf_assumptions)")
            }
            if not columns:
                return _StoredRowsRead({}, "missing_schema")
            required = {"field", "value", "updated_at"}
            if not required.issubset(columns):
                return _StoredRowsRead(
                    {},
                    "invalid_schema",
                    f"missing columns: {sorted(required - columns)}",
                )
            rows = conn.execute(
                "SELECT field, value, updated_at FROM global_dcf_assumptions"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "global_dcf_assumptions_read_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return _StoredRowsRead({}, "read_failed", f"{type(exc).__name__}: {exc}")
    out: dict[str, tuple[float, str | None]] = {}
    for field, value, updated_at in rows:
        if field in KNOWN_FIELDS and value is not None:
            out[str(field)] = (
                float(value),
                str(updated_at) if updated_at is not None else None,
            )
    return _StoredRowsRead(out, "ok")


def _read_stored(db_path: Path | str | None) -> dict[str, float]:
    """Return {field: value} for the rows actually present in the table.
    Empty dict when the DB / table is missing or unreadable (fails to seed
    defaults at the caller)."""
    result = _read_stored_rows(db_path)
    return {field: value for field, (value, _updated_at) in result.rows.items()}


def get(field: str, *, db_path: Path | str | None = None) -> float | None:
    """The STORED global value for ``field`` (None when no row / no DB). Use
    this to ask "does the global store explicitly set this?"; use ``resolve``
    or ``load`` to get a value you can compute with."""
    return _read_stored(db_path).get(field)


def get_all(*, db_path: Path | str | None = None) -> dict[str, float]:
    """Effective map for every known field: seed defaults overlaid with stored
    values. What the dashboard panel renders."""
    merged = dict(SEED_DEFAULTS)
    merged.update(_read_stored(db_path))
    return merged


def load(*, db_path: Path | str | None = None) -> GlobalDcfAssumptions:
    """Effective global assumptions as a typed object — every field resolved
    (stored value or seed default). The builders' entry point."""
    eff = get_all(db_path=db_path)
    return GlobalDcfAssumptions(
        risk_free_rate=eff["risk_free_rate"],
        equity_risk_premium=eff["equity_risk_premium"],
        tax_rate=eff["tax_rate"],
    )


def load_with_provenance(*, db_path: Path | str | None = None) -> GlobalDcfAssumptionsLoad:
    """Load effective globals with exact database/seed lineage in one read."""
    read = _read_stored_rows(db_path)
    stored = read.rows
    effective = dict(SEED_DEFAULTS)
    effective.update({field: value for field, (value, _updated_at) in stored.items()})
    assumptions = GlobalDcfAssumptions(
        risk_free_rate=effective["risk_free_rate"],
        equity_risk_premium=effective["equity_risk_premium"],
        tax_rate=effective["tax_rate"],
    )
    observed: list[datetime] = []
    fields: list[dict[str, object]] = []
    for field in sorted(KNOWN_FIELDS):
        stored_row = stored.get(field)
        updated_at = stored_row[1] if stored_row is not None else None
        if updated_at:
            try:
                parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                observed.append(
                    parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
                )
        fields.append(
            {
                "field": field,
                "value": effective[field],
                "source": "database" if stored_row is not None else "seed_default",
                "updated_at": updated_at,
            }
        )
    if read.status != "ok":
        status = read.status
    elif not stored:
        status = "seed_default"
    elif len(stored) == len(KNOWN_FIELDS):
        status = "database"
    else:
        status = "mixed"
    return GlobalDcfAssumptionsLoad(
        assumptions=assumptions,
        source_record={
            "role": "global_dcf_assumptions",
            "locator": "data/portfolio.db:global_dcf_assumptions",
            "status": status,
            "read_detail": read.detail,
            "observed_at": max(observed).isoformat() if observed else None,
            "fields": fields,
        },
    )


def capm_ke(
    beta: float,
    *,
    country_risk_premium: float = 0.0,
    db_path: Path | str | None = None,
) -> float:
    """Cost of equity from CAPM on the global risk-free + ERP:
    ``rf + beta * erp + country_risk_premium``.

    The opt-in path by which the non-CAPM models (fintech SOTP, NU platform)
    let the editable global risk-free / ERP drive their discount rate. Off by
    default in those builders, so each model's explicit ``ke`` is unchanged
    until a ticker asks for the CAPM derivation. Degrades to the seed defaults
    when the store is absent."""
    g = load(db_path=db_path)
    return g.risk_free_rate + float(beta) * g.equity_risk_premium + float(country_risk_premium)


def resolve(
    field: str,
    *,
    ticker_value: float | int | None = None,
    fallback: float | None = None,
    db_path: Path | str | None = None,
) -> float:
    """Precedence resolver: **ticker_value > global default > fallback > seed**.

    ``ticker_value`` is the per-ticker override (e.g. ``opus.get(field)`` or a
    value from a model's assumption JSON) — when not None it wins outright.
    Otherwise the stored global default; otherwise the model's historical
    ``fallback`` literal; otherwise the in-code seed default. Raises KeyError
    only when ``field`` is unknown AND no fallback is given.
    """
    if ticker_value is not None:
        return float(ticker_value)
    stored = get(field, db_path=db_path)
    if stored is not None:
        return stored
    if fallback is not None:
        return float(fallback)
    if field in SEED_DEFAULTS:
        return SEED_DEFAULTS[field]
    raise KeyError(f"unknown global DCF assumption field {field!r} and no fallback given")


def _validate(field: str, value: float) -> float:
    if field not in KNOWN_FIELDS:
        raise ValueError(
            f"unknown global DCF assumption field {field!r}; expected one of {sorted(KNOWN_FIELDS)}"
        )
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value for {field!r} must be a number, got {value!r}") from exc
    if not (_MIN_VALUE <= v <= _MAX_VALUE):
        raise ValueError(
            f"value for {field!r} must be a decimal ratio in "
            f"[{_MIN_VALUE}, {_MAX_VALUE}] (e.g. 0.043 = 4.3%), got {v}"
        )
    return v


def set_value(
    field: str,
    value: float,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Upsert one global field. Returns True on write, False when the DB is
    unavailable. Raises ValueError on an unknown field or an out-of-range value
    (the POST route maps that to a 400). Creates the row if absent, so the
    store is editable even before the seed migration's row exists."""
    v = _validate(field, value)
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    updated_at = (now or datetime.now(UTC).replace(tzinfo=None)).isoformat()
    try:
        conn = connect_sqlite(
            path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        try:
            cur = conn.execute(
                """
                INSERT INTO global_dcf_assumptions (field, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(field) DO UPDATE
                  SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (field, v, updated_at),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "global_dcf_assumptions_write_failed",
                "field": field,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False

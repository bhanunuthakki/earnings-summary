"""FMP snapshot ingest + parity/QA drift check for the bottoms-up
comparable-set program (docs/design/comparable_sets_bottoms_up.md §7,
Phase 2).

Two responsibilities, both deterministic, no LLM:

1. **Snapshot ingest** — re-key FMP's ``sector-pe-snapshot`` /
   ``industry-pe-snapshot`` cache files
   (``data/historical/sector_industry/{kind}_pe_snapshot.json``, written by
   ``execution/save_fmp_data.py::run_sector_industry``) into
   ``comp_set_metrics_daily`` as ``scope_type='fmp_snapshot'`` rows so the
   drift check is a same-table join, not a cross-format comparison (§6 note).
   ``stat_type`` is always ``'median'`` — FMP's snapshot publishes no
   cap-weighted variant, and the drift check only ever compares FMP's
   median-shaped number against our ``median`` row, never ``aggregate``
   (comparing different constructions would manufacture fake drift).
   Snapshot rows carry a ``vendor_field`` locator (per-#911 rule: the honest
   floor for a plain vendor endpoint value).

   Observed file shape (verified against the production cache): a flat list
   of ``{date, industry|sector, exchange, pe}`` — currently one exchange
   (NASDAQ) per key, but grouped defensively: multiple exchange rows for one
   key on one date combine as their median, with the exchange list recorded
   in ``method_flags`` so a multi-exchange snapshot is visible, not silently
   averaged away.

2. **Drift computation** — for each pool industry/sector with both a
   bottoms-up ``scope_type='industry'``/``'sector'`` median-PE row and a
   ``scope_type='fmp_snapshot'`` row on the same or nearest **prior**
   ``as_of_date``: ``drift_pct = (bottoms_up - fmp) / fmp``. No dedicated
   drift table (§13: query both scope_types from comp_set_metrics_daily
   directly). Beyond ``DRIFT_ALERT_THRESHOLD`` is a data-quality *signal*
   for the owner (structured warning + a ``validation_issues`` row with the
   existing ``source_disagreement`` rule, written by the CLI), never a hard
   failure — §7's documented expected deviations (universe composition, TTM
   cutoff timing) make moderate drift normal, not a bug.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import cast

from compute.comp_set_metrics import MetricRow, upsert_metric_rows
from pipeline.locators import vendor_field_locator

# §7: beyond this |drift|, without an explainable composition reason, the
# check surfaces a data-quality flag. Named, greppable knob — not a magic
# number. Alert fires strictly beyond the threshold.
DRIFT_ALERT_THRESHOLD = 0.25

_SNAPSHOT_KINDS = ("industry", "sector")


# ---------------------------------------------------------------------------
# 1. snapshot ingest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotEntry:
    """One (scope_key, date) from an FMP PE-snapshot file, exchange rows
    already combined (median) — see module docstring."""

    kind: str  # 'industry'|'sector'
    scope_key: str
    as_of: date
    pe: float
    exchanges: tuple[str, ...]


def load_fmp_pe_snapshot(repo_root: Path, kind: str) -> list[SnapshotEntry]:
    """Parse ``data/historical/sector_industry/{kind}_pe_snapshot.json``.

    Missing file / malformed JSON returns ``[]`` (the snapshot is an optional
    QA reference — ``run_sector_industry`` may simply never have run on this
    checkout); rows lacking a parseable date/key/pe are skipped, never
    fabricated.
    """
    if kind not in _SNAPSHOT_KINDS:
        raise ValueError(f"kind must be one of {_SNAPSHOT_KINDS}, got {kind!r}")
    path = repo_root / "data" / "historical" / "sector_industry" / f"{kind}_pe_snapshot.json"
    if not path.exists():
        return []
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []

    grouped: dict[tuple[str, date], list[tuple[float, str]]] = {}
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        rec = cast("dict[str, object]", item)
        key = rec.get(kind)  # 'industry' or 'sector' field name matches kind
        pe = rec.get("pe")
        raw_date = rec.get("date")
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(pe, (int, float)):
            continue
        if not isinstance(raw_date, str):
            continue
        try:
            as_of = date.fromisoformat(raw_date)
        except ValueError:
            continue
        exchange = rec.get("exchange")
        grouped.setdefault((key.strip(), as_of), []).append(
            (float(pe), exchange.strip() if isinstance(exchange, str) else "")
        )

    entries: list[SnapshotEntry] = []
    for (key, as_of), rows in sorted(grouped.items()):
        entries.append(
            SnapshotEntry(
                kind=kind,
                scope_key=key,
                as_of=as_of,
                pe=statistics.median(pe for pe, _ in rows),
                exchanges=tuple(sorted({ex for _, ex in rows if ex})),
            )
        )
    return entries


def upsert_fmp_snapshot_rows(
    conn: sqlite3.Connection,
    entries: list[SnapshotEntry],
    method_version: int,
) -> int:
    """Write snapshot entries as ``scope_type='fmp_snapshot'`` rows.

    Idempotent via the table's unique constraint (same upsert path as every
    other row). ``n_members``/``n_valid`` count the exchange rows that
    contributed (coverage of *this file*, honestly 1.0 — FMP's own universe
    size is not published and never guessed). ``method_flags.snapshot_kind``
    disambiguates an industry key from a sector key at drift-join time.
    """
    n = 0
    for entry in entries:
        flags: dict[str, object] = {"snapshot_kind": entry.kind}
        if entry.exchanges:
            flags["exchanges"] = list(entry.exchanges)
        row = MetricRow(
            metric="pe_ttm",
            stat_type="median",
            value=entry.pe,
            n_members=1,
            n_valid=1,
            coverage_pct=1.0,
            method_flags=flags,
            locator=vendor_field_locator(
                endpoint=f"{entry.kind}-pe-snapshot",
                field="pe",
                period=entry.as_of.isoformat(),
            ),
        )
        n += upsert_metric_rows(
            conn, "fmp_snapshot", entry.scope_key, entry.as_of, method_version, [row]
        )
    return n


# ---------------------------------------------------------------------------
# 2. drift computation
# ---------------------------------------------------------------------------


@dataclass
class DriftResult:
    scope_type: str  # 'industry'|'sector'
    scope_key: str
    as_of_date: date  # bottoms-up row's date
    bottoms_up_median: float
    fmp_median: float
    fmp_as_of: date
    drift_pct: float  # (bottoms_up - fmp) / fmp
    n_members: int  # bottoms-up universe size (for the §7 one-glance composition check)
    n_valid: int
    snapshot_age_days: int
    alert: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "as_of_date": self.as_of_date.isoformat(),
            "bottoms_up_median": self.bottoms_up_median,
            "fmp_median": self.fmp_median,
            "fmp_as_of": self.fmp_as_of.isoformat(),
            "drift_pct": round(self.drift_pct, 4),
            "n_members": self.n_members,
            "n_valid": self.n_valid,
            "snapshot_age_days": self.snapshot_age_days,
            "alert": self.alert,
        }


@dataclass
class DriftReport:
    as_of_date: date | None  # bottoms-up comparison date actually used (None = no rows)
    results: list[DriftResult] = field(default_factory=list[DriftResult])
    skipped: list[dict[str, object]] = field(default_factory=list[dict[str, object]])


def latest_scope_date(
    conn: sqlite3.Connection, scope_type: str, on_or_before: date, method_version: int
) -> date | None:
    """Most recent as_of_date <= `on_or_before` with bottoms-up median-PE rows
    for `scope_type` — the comparison date when the drift check runs on a day
    track_comp_metrics hasn't (yet)."""
    cur = conn.execute(
        "SELECT MAX(as_of_date) FROM comp_set_metrics_daily "
        "WHERE scope_type = ? AND metric = 'pe_ttm' AND stat_type = 'median' "
        "AND method_version = ? AND as_of_date <= ?",
        (scope_type, method_version, on_or_before.isoformat()),
    )
    row = cur.fetchone()
    raw = row[0] if row is not None else None
    return date.fromisoformat(str(raw)) if raw else None


def _nearest_prior_snapshot(
    conn: sqlite3.Connection,
    scope_key: str,
    kind: str,
    on_or_before: date,
    method_version: int,
) -> tuple[date, float] | None:
    cur = conn.execute(
        "SELECT as_of_date, value, method_flags FROM comp_set_metrics_daily "
        "WHERE scope_type = 'fmp_snapshot' AND scope_key = ? AND metric = 'pe_ttm' "
        "AND stat_type = 'median' AND method_version = ? AND as_of_date <= ? "
        "AND value IS NOT NULL ORDER BY as_of_date DESC",
        (scope_key, method_version, on_or_before.isoformat()),
    )
    for row in cur.fetchall():
        flags_raw = row["method_flags"]
        if isinstance(flags_raw, str) and flags_raw.strip():
            try:
                flags = cast("dict[str, object]", json.loads(flags_raw))
            except json.JSONDecodeError:
                flags = {}
            snapshot_kind = flags.get("snapshot_kind")
            if isinstance(snapshot_kind, str) and snapshot_kind != kind:
                continue  # an industry named like a sector (or vice versa)
        return date.fromisoformat(str(row["as_of_date"])), float(row["value"])
    return None


def compute_drift(
    conn: sqlite3.Connection,
    scope_type: str,
    as_of: date,
    method_version: int,
) -> DriftReport:
    """§7: median-vs-median PE drift for every `scope_type` key with both a
    bottoms-up row (at the latest date <= `as_of`) and a nearest-prior
    fmp_snapshot row. Undefined/NULL medians and non-positive FMP medians are
    skipped with an explicit reason, never coerced."""
    if scope_type not in _SNAPSHOT_KINDS:
        raise ValueError(f"scope_type must be one of {_SNAPSHOT_KINDS}, got {scope_type!r}")
    report_date = latest_scope_date(conn, scope_type, as_of, method_version)
    report = DriftReport(as_of_date=report_date)
    if report_date is None:
        return report

    cur = conn.execute(
        "SELECT scope_key, value, n_members, n_valid FROM comp_set_metrics_daily "
        "WHERE scope_type = ? AND metric = 'pe_ttm' AND stat_type = 'median' "
        "AND method_version = ? AND as_of_date = ? ORDER BY scope_key",
        (scope_type, method_version, report_date.isoformat()),
    )
    for row in cur.fetchall():
        scope_key = str(row["scope_key"])
        value = row["value"]
        if value is None:
            report.skipped.append({"scope_key": scope_key, "reason": "bottoms_up_median_null"})
            continue
        snapshot = _nearest_prior_snapshot(conn, scope_key, scope_type, report_date, method_version)
        if snapshot is None:
            report.skipped.append({"scope_key": scope_key, "reason": "no_fmp_snapshot_row"})
            continue
        fmp_as_of, fmp_median = snapshot
        if fmp_median <= 0:
            report.skipped.append({"scope_key": scope_key, "reason": "fmp_median_non_positive"})
            continue
        bottoms_up = float(value)
        drift_pct = (bottoms_up - fmp_median) / fmp_median
        report.results.append(
            DriftResult(
                scope_type=scope_type,
                scope_key=scope_key,
                as_of_date=report_date,
                bottoms_up_median=bottoms_up,
                fmp_median=fmp_median,
                fmp_as_of=fmp_as_of,
                drift_pct=drift_pct,
                n_members=int(row["n_members"]),
                n_valid=int(row["n_valid"]),
                snapshot_age_days=(report_date - fmp_as_of).days,
                alert=abs(drift_pct) > DRIFT_ALERT_THRESHOLD,
            )
        )
    return report

"""Execute a ViewSpec against the fact tables (master build P5.1).

Deterministic and LLM-free: every cell comes from the canonical tier-aware
loaders in ``timeseries.loaders`` (the same row picks the reports use) and
every fin/kpi cell carries the provenance of its winning fact row, so the
renderer can chip each number. Segment cells render unchipped for now —
the junction's provenance is period-level and joins documents through
segment_periods; wire it when a surface needs it.

Cross-ticker alignment is by CALENDAR bucket: a quarterly view buckets
each observation into (calendar year, calendar quarter) derived from its
fiscal period_end, so an offset fiscal calendar (AAPL's December "Q1")
lands in the calendar quarter it actually ended in. That is the honest
axis for cross-ticker comparison; the bucket label ("Q4'25") therefore may
differ from the issuer's own fiscal quarter name. Annual views bucket FY
rows by period_end year.

Transforms (the spec's vocabulary):
  level  — the raw value
  yoy    — % change vs the same bucket one year earlier
  cagr   — trailing ``spec.cagr_years``-year CAGR ending at each bucket
  margin — value / fin:revenue (same ticker, same bucket), in %
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from compute.kpi_resolver import kpi_group_key
from provenance.overrides import FactOverride, get_active_overrides, override_provenance
from report.models import CellSource
from timeseries.loaders import (
    SourcedObservation,
    load_financial_series,
    load_financial_series_with_provenance,
    load_kpi_series_with_provenance,
    load_segment_junction_series_with_provenance,
)
from viewspec.spec import MetricRef, ViewSpec

log = logging.getLogger(__name__)

# (TICKER, fact_kind, fact_key) -> active replace overrides for that scalar
# fact. Read once per view so override-only injection is an in-memory lookup.
_ScalarOverrideMap = dict[tuple[str, str, str], list[FactOverride]]

# TICKER -> {kpi_group_key -> that ticker's richest stored KPI name for the
# group}. Lets a de-fragmented representative token (metric_catalog groups KPI
# surface variants into one) resolve back to each ticker's own variant. Read
# once per view.
_KpiResolution = dict[str, dict[str, str]]

# The DIY picker reads this catalog live per selected ticker; with the
# "capture every reported number" program the long tail can run to many
# hundreds of facts per name. The picker UI carries a type-ahead search
# (explore_panel.py), so a large list is usable — this bound is a safety
# ceiling against a pathological scan, NOT a UX cap. Kept generous so no
# extracted fact is silently truncated out of the picker.
_CATALOG_LIMIT_PER_DOMAIN = 2000

# MetricRef.domain -> fact_overrides.fact_kind, for the scalar read paths that
# surface override-only facts (a company-doc figure FMP never carried).
_DOMAIN_TO_OVERRIDE_KIND: dict[str, str] = {"fin": "financial_fact", "kpi": "kpi"}
_SCALAR_OVERRIDE_KINDS = frozenset(_DOMAIN_TO_OVERRIDE_KIND.values())

_QUARTERLY_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")
_ANNUAL_PERIOD_TYPES: tuple[str, ...] = ("FY",)

# (calendar year, quarter 1..4) for quarterly views; (year, 0) for annual —
# one orderable key shape either way.
_Bucket = tuple[int, int]


@dataclass(slots=True)
class ViewCell:
    """One rendered cell: the transformed value, the underlying level, and
    the provenance of the level's winning fact row (None for seg cells and
    for buckets the transform could not compute)."""

    value: float | None
    raw: float | None
    source: CellSource | None


@dataclass(slots=True)
class ViewRow:
    """One pivot row — a (ticker, metric) series across the period axis."""

    ticker: str
    metric: MetricRef
    label: str
    unit: str | None
    cells: list[ViewCell]


@dataclass(slots=True)
class ViewResult:
    """What `execute_view` hands the renderer."""

    spec: ViewSpec
    period_labels: list[str]
    rows: list[ViewRow]
    warnings: list[str]
    # {metric token: one-line definition} — row-label tooltips (Ask v4).
    # Populated by execute_view from viewspec.glossary; default keeps every
    # hand-constructed ViewResult (tests, embeds) valid.
    definitions: dict[str, str] = field(default_factory=dict[str, str])


def _bucket_label(bucket: _Bucket, cadence: str) -> str:
    year, q = bucket
    if cadence == "annual" or q == 0:
        return f"FY{year}"
    return f"Q{q}'{str(year)[2:]}"


def _cell_source(prov: dict[str, object]) -> CellSource:
    """SourcedObservation.provenance → the chip model. ``source_doc_id`` is
    documents.id, which is exactly what CellSource.doc_id deep-links."""

    def _s(key: str) -> str | None:
        v = prov.get(key)
        return str(v) if v is not None else None

    doc_raw = prov.get("source_doc_id")
    fact_raw = prov.get("fact_id")
    conf_raw = prov.get("confidence")
    fact_table_raw = prov.get("fact_table")
    return CellSource(
        source=str(prov.get("source") or "unknown"),
        fetched_at=_s("fetched_at"),
        source_url=_s("source_url"),
        doc_type=_s("doc_type"),
        accession_number=_s("accession_number"),
        filing_date=_s("filing_date"),
        locator=_s("locator"),
        doc_id=doc_raw if isinstance(doc_raw, int) else None,
        fact_id=fact_raw if isinstance(fact_raw, int) else None,
        fact_table=(
            fact_table_raw
            if isinstance(fact_table_raw, str) and fact_table_raw
            else "financial_facts"
        ),
        confidence=float(conf_raw) if isinstance(conf_raw, (int, float)) else None,
        extracted_by=_s("extracted_by"),
        computed_from=_s("computed_from"),
    )


def _load_row_data(
    ticker: str,
    metric: MetricRef,
    cadence: str,
    *,
    db_path: Path | None,
    repo_root: Path | None,
    overrides: _ScalarOverrideMap,
    kpi_resolution: _KpiResolution,
) -> tuple[dict[_Bucket, ViewCell], str | None]:
    """One (ticker, metric) series as {bucket: level cell} + the unit hint.

    Within a bucket the later period_end wins (a fiscal-calendar change can
    land two fiscal periods in one calendar bucket; the loaders return
    ascending series so the natural overwrite is the later one).
    """
    period_types = _ANNUAL_PERIOD_TYPES if cadence == "annual" else _QUARTERLY_PERIOD_TYPES
    cells: dict[_Bucket, ViewCell] = {}
    unit: str | None = None
    if metric.domain in ("fin", "kpi"):
        sourced: list[SourcedObservation]
        if metric.domain == "fin":
            load_key = metric.key
            sourced = load_financial_series_with_provenance(
                ticker, load_key, repo_root, db_path=db_path, period_types=period_types
            )
        else:
            # The kpi token is a de-fragmented representative — resolve it to
            # THIS ticker's own variant (richest series) before loading, so a
            # grouped token pulls each ticker's data even when their surface
            # spellings differ (acceptance §7a.3). Falls back to the literal key
            # when nothing resolves (then the loader simply returns []).
            load_key = kpi_resolution.get(ticker.upper(), {}).get(
                kpi_group_key(metric.key), metric.key
            )
            sourced = load_kpi_series_with_provenance(
                ticker, load_key, repo_root, db_path=db_path, period_types=period_types
            )
        for ob in sourced:
            b = _to_bucket(ob.period_end.year, ob.period_end.month, cadence)
            cells[b] = ViewCell(value=None, raw=ob.value, source=_cell_source(ob.provenance))
            if ob.unit:
                unit = ob.unit
        # Override-only facts (a company-doc figure FMP never carried) are now
        # pickable — metric_catalog unions fact_overrides — so picking one must
        # not come back empty. The loaders' overlay only rewrites EXISTING base
        # rows; this injects the periods that exist solely as a replace
        # override. Keyed on the resolved series name so a kpi override lands on
        # the same series the loader read. Localized to the viewspec read path.
        _inject_override_only(
            cells,
            ticker=ticker,
            fact_kind=_DOMAIN_TO_OVERRIDE_KIND[metric.domain],
            fact_key=load_key,
            cadence=cadence,
            period_types=period_types,
            overrides=overrides,
        )
        return cells, unit
    # seg: period-level provenance — the junction joins documents through
    # segment_periods.source_doc_id, so each cell chips its source document.
    dims = [(metric.dim_type or "", metric.dim_name or "")]
    seg_sourced = load_segment_junction_series_with_provenance(
        ticker, dims, metric.key, repo_root, db_path=db_path, period_types=period_types
    )
    for obs in seg_sourced:
        b = _to_bucket(obs.period_end.year, obs.period_end.month, cadence)
        cells[b] = ViewCell(value=None, raw=obs.value, source=_cell_source(obs.provenance))
        if obs.unit:
            unit = obs.unit
    return cells, unit


def _inject_override_only(
    cells: dict[_Bucket, ViewCell],
    *,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    cadence: str,
    period_types: tuple[str, ...],
    overrides: _ScalarOverrideMap,
) -> None:
    """Add cells for periods that exist ONLY as an active ``replace`` override.

    The scalar loaders already overlaid overrides onto periods with a base row
    (so ``cells`` reflects them); this handles the override-only case the
    overlay can't reach — no base row to rewrite. A pure in-memory lookup
    against the per-view ``overrides`` map (built once by
    :func:`_active_scalar_overrides`); a no-op when there are none.
    """
    for ov in overrides.get((ticker.upper(), fact_kind, fact_key), ()):
        if ov.fiscal_period_type not in period_types or ov.value is None:
            continue
        try:
            year, month = int(ov.period_end[:4]), int(ov.period_end[5:7])
        except ValueError:
            continue
        bucket = _to_bucket(year, month, cadence)
        if bucket in cells:  # the base row already carried (and overlaid) it
            continue
        cells[bucket] = ViewCell(
            value=None, raw=float(ov.value), source=_cell_source(override_provenance(ov))
        )


def _active_scalar_overrides(
    tickers: tuple[str, ...],
    *,
    db_path: Path | None,
    repo_root: Path | None,
) -> _ScalarOverrideMap:
    """Active ``replace`` overrides for fin/kpi facts across ``tickers``, keyed
    ``(TICKER, fact_kind, fact_key)`` — read ONCE per view so override-only
    injection is an in-memory lookup, not a per-cell DB hit. Best-effort: a
    missing DB / ``fact_overrides`` table yields an empty map (the common case).
    """
    resolved = db_path
    if resolved is None and repo_root is not None:
        resolved = repo_root / "data" / "portfolio.db"
    if resolved is None or not resolved.exists():
        return {}
    try:
        conn = sqlite3.connect(str(resolved), timeout=5.0)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    out: _ScalarOverrideMap = {}
    try:
        for ticker in dict.fromkeys(t.upper() for t in tickers):
            for ov in get_active_overrides(conn, ticker=ticker):
                if (
                    ov.fact_kind in _SCALAR_OVERRIDE_KINDS
                    and ov.action == "replace"
                    and ov.value is not None
                ):
                    out.setdefault((ticker, ov.fact_kind, ov.fact_key), []).append(ov)
    finally:
        conn.close()
    return out


def _kpi_name_resolution(
    spec: ViewSpec,
    *,
    db_path: Path | None,
    repo_root: Path | None,
) -> _KpiResolution:
    """``{TICKER: {kpi_group_key: that ticker's richest stored name}}`` so a
    de-fragmented representative token resolves to each ticker's own variant.

    Read ONCE per view (one query per ticker) and only when the spec carries a
    kpi metric — a no-op otherwise. Within a ticker's group the name with the
    MOST observations wins (load the fullest series), ties broken shortest then
    alphabetical. Best-effort: missing DB / kpi tables yield an empty map (the
    engine then loads each kpi token literally).
    """
    if not any(m.domain == "kpi" for m in spec.metrics):
        return {}
    resolved = db_path
    if resolved is None and repo_root is not None:
        resolved = repo_root / "data" / "portfolio.db"
    if resolved is None or not resolved.exists():
        return {}
    try:
        conn = sqlite3.connect(str(resolved), timeout=5.0)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    out: _KpiResolution = {}
    try:
        for ticker in dict.fromkeys(t.upper() for t in spec.tickers):
            try:
                rows = conn.execute(
                    "SELECT kd.name AS name, COUNT(*) AS n "
                    "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
                    "WHERE kf.ticker = ? GROUP BY kd.name",
                    (ticker,),
                ).fetchall()
            except sqlite3.Error:
                continue
            by_key: dict[str, list[tuple[str, int]]] = {}
            for r in rows:
                name = str(r["name"])
                by_key.setdefault(kpi_group_key(name), []).append((name, int(r["n"])))
            out[ticker] = {
                key: min(cands, key=lambda c: (-c[1], len(c[0]), c[0]))[0]
                for key, cands in by_key.items()
            }
    finally:
        conn.close()
    return out


def _to_bucket(year: int, month: int, cadence: str) -> _Bucket:
    if cadence == "annual":
        return (year, 0)
    return (year, (month - 1) // 3 + 1)


def _lookback(bucket: _Bucket, years: int) -> _Bucket:
    return (bucket[0] - years, bucket[1])


def execute_view(
    spec: ViewSpec,
    *,
    db_path: Path | None = None,
    repo_root: Path | None = None,
) -> ViewResult:
    """Run the spec. Best-effort like the loaders underneath: rows with no
    data become warnings, never exceptions; an unreachable DB yields an
    empty result."""
    warnings: list[str] = []
    raw_rows: list[tuple[str, MetricRef, dict[_Bucket, ViewCell], str | None]] = []
    # Read the per-view lookups ONCE (in-memory per cell, not a per-cell DB
    # hit): the override-only scalar facts, and the kpi de-fragmentation
    # resolution (representative token -> each ticker's own variant).
    overrides = _active_scalar_overrides(spec.tickers, db_path=db_path, repo_root=repo_root)
    kpi_resolution = _kpi_name_resolution(spec, db_path=db_path, repo_root=repo_root)
    # Metric-major ordering: the same metric's tickers sit adjacent, which is
    # the comparison the pivot exists for.
    for metric in spec.metrics:
        for ticker in spec.tickers:
            cells, unit = _load_row_data(
                ticker,
                metric,
                spec.cadence,
                db_path=db_path,
                repo_root=repo_root,
                overrides=overrides,
                kpi_resolution=kpi_resolution,
            )
            if not cells:
                warnings.append(f"{ticker}: no data for {metric.token()}")
                continue
            raw_rows.append((ticker, metric, cells, unit))

    # Margin divisor: fin:revenue per ticker, loaded once.
    revenue_by_ticker: dict[str, dict[_Bucket, float]] = {}
    if spec.transform == "margin":
        period_types = _ANNUAL_PERIOD_TYPES if spec.cadence == "annual" else _QUARTERLY_PERIOD_TYPES
        for ticker in spec.tickers:
            rev = load_financial_series(
                ticker, "revenue", repo_root, db_path=db_path, period_types=period_types
            )
            revenue_by_ticker[ticker] = {
                _to_bucket(o.period_end.year, o.period_end.month, spec.cadence): o.value
                for o in rev
            }
            if not revenue_by_ticker[ticker]:
                warnings.append(f"{ticker}: no fin:revenue series — margin cells empty")

    all_buckets: set[_Bucket] = set()
    for _t, _m, cells, _u in raw_rows:
        all_buckets.update(cells)
    display_buckets = sorted(all_buckets)[-spec.periods :]

    rows: list[ViewRow] = []
    for ticker, metric, cells, unit in raw_rows:
        out_cells: list[ViewCell] = []
        for b in display_buckets:
            cell = cells.get(b)
            raw = cell.raw if cell is not None else None
            src = cell.source if cell is not None else None
            value: float | None = None
            if raw is not None:
                if spec.transform == "level":
                    value = raw
                elif spec.transform == "yoy":
                    prior_cell = cells.get(_lookback(b, 1))
                    prior = prior_cell.raw if prior_cell is not None else None
                    if prior is not None and prior != 0:
                        value = (raw / prior - 1) * 100
                elif spec.transform == "cagr":
                    base_cell = cells.get(_lookback(b, spec.cagr_years))
                    base = base_cell.raw if base_cell is not None else None
                    if base is not None and base > 0 and raw > 0:
                        value = ((raw / base) ** (1 / spec.cagr_years) - 1) * 100
                elif spec.transform == "margin":
                    rev = revenue_by_ticker.get(ticker, {}).get(b)
                    if rev is not None and rev != 0:
                        value = raw / rev * 100
            out_cells.append(ViewCell(value=value, raw=raw, source=src))
        rows.append(
            ViewRow(
                ticker=ticker,
                metric=metric,
                label=f"{ticker} · {metric.label}",
                unit=unit,
                cells=out_cells,
            )
        )

    from viewspec.glossary import metric_definitions

    resolved_db = db_path
    if resolved_db is None and repo_root is not None:
        resolved_db = repo_root / "data" / "portfolio.db"
    return ViewResult(
        spec=spec,
        period_labels=[_bucket_label(b, spec.cadence) for b in display_buckets],
        rows=rows,
        warnings=warnings,
        definitions=metric_definitions(spec.metrics, db_path=resolved_db, tickers=spec.tickers),
    )


# ---------------------------------------------------------------------------
# Metric catalog (the builder UI's picker content)
# ---------------------------------------------------------------------------


def metric_catalog(
    db_path: Path,
    tickers: list[str],
    *,
    limit_per_domain: int = _CATALOG_LIMIT_PER_DOMAIN,
) -> dict[str, list[dict[str, object]]]:
    """What can be plotted for these tickers: distinct financial line items,
    KPI names (with at least one fact row), and segment slices — each as
    ``{"token": ..., "label": ..., "tickers": n}`` ordered by how many of
    the requested tickers carry it.

    The KPI domain is DE-FRAGMENTED (acceptance §7a.3): surface variants of one
    metric — across AND within tickers — collapse onto a single comparable
    token via :func:`compute.kpi_resolver.kpi_group_key` (the same leaf
    normalization the ask name-match uses), so a typed/picked metric isn't
    split across spellings. KPI entries also carry ``origin`` (``analyst``
    curated vs ``capture`` auto-minted, from ``kpi_definitions.definition_origin``
    when present — see S1 / migration 0113). Override-only facts (a company-doc
    figure FMP never carried) are unioned in from ``fact_overrides`` so they are
    pickable too. Best-effort: missing DB/tables/columns degrade gracefully
    (empty lists / omitted ``origin``).
    """
    out: dict[str, list[dict[str, object]]] = {"fin": [], "kpi": [], "seg": []}
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    if not symbols or not db_path.exists():
        return out
    marks = ",".join("?" * len(symbols))
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return out
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            out["fin"] = _catalog_query(
                conn,
                f"""
                SELECT line_item AS k, COUNT(DISTINCT ticker) AS n
                FROM financial_facts WHERE ticker IN ({marks})
                GROUP BY line_item ORDER BY n DESC, k ASC LIMIT ?
                """,
                (*symbols, limit_per_domain),
                lambda r: {
                    "token": f"fin:{r['k']}",
                    "label": str(r["k"]),
                    "tickers": int(r["n"]),
                },
            )
            out["kpi"] = _grouped_kpi_catalog(conn, marks, symbols, limit_per_domain)
            out["seg"] = _catalog_query(
                conn,
                f"""
                SELECT sd.dim_type AS dt, sd.dim_name AS dn, sd.metric AS m,
                       COUNT(DISTINCT sp.ticker) AS n
                FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id
                WHERE sp.ticker IN ({marks})
                GROUP BY dt, dn, m ORDER BY n DESC, dn ASC, m ASC LIMIT ?
                """,
                (*symbols, limit_per_domain),
                lambda r: {
                    "token": f"seg:{r['dt']}:{r['dn']}:{r['m']}",
                    "label": f"{r['dn']} {r['m']} ({r['dt']})",
                    "tickers": int(r["n"]),
                },
            )
            # Honesty guard (acceptance criterion: never present a truncated
            # list as complete). The cap is generous (well past a metric-rich
            # ticker's real count), so this should never fire — but if a domain
            # ever hits it, the long tail WAS cut, so say so rather than imply
            # completeness. The base lists are what the cap governs; the
            # override union is additive on top.
            for domain in ("fin", "kpi", "seg"):
                if len(out[domain]) >= limit_per_domain:
                    log.warning(
                        {
                            "event": "metric_catalog_domain_truncated",
                            "domain": domain,
                            "limit": limit_per_domain,
                            "tickers": symbols,
                        }
                    )
            _union_override_only(conn, out, symbols, marks, limit_per_domain)
    finally:
        conn.close()
    # Definition tooltips (Ask v4) — picker <option title> text per entry.
    from viewspec.glossary import attach_catalog_titles

    attach_catalog_titles(out, db_path=db_path, tickers=symbols)
    return out


def _grouped_kpi_catalog(
    conn: sqlite3.Connection,
    marks: str,
    symbols: list[str],
    limit_per_domain: int,
) -> list[dict[str, object]]:
    """The KPI domain, de-fragmented by :func:`kpi_group_key` (acceptance §7a.3).

    Surface variants of one metric — across AND within tickers — collapse to a
    single entry. The representative (the token + label) is the SHORTEST variant
    (the cleanest leaf), most-observations breaking ties; ``tickers`` counts the
    DISTINCT tickers across the whole group; ``origin`` is ``analyst`` if ANY
    member is curated, else ``capture`` (when ``definition_origin`` exists —
    migration 0113). The token stays per-ticker resolvable: at execution
    :func:`_kpi_name_resolution` maps the group key back to each ticker's own
    variant. Best-effort: a missing kpi table yields ``[]``.
    """
    has_origin = _column_exists(conn, "kpi_definitions", "definition_origin")
    origin_select = ", kd.definition_origin AS origin" if has_origin else ""
    try:
        rows = conn.execute(
            f"""
            SELECT kd.name AS name, kf.ticker AS ticker, COUNT(*) AS obs{origin_select}
            FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker IN ({marks})
            GROUP BY kd.name, kf.ticker
            """,
            tuple(symbols),
        ).fetchall()
    except sqlite3.Error:
        return []
    obs_by_name: dict[str, dict[str, int]] = {}
    tickers_by_key: dict[str, set[str]] = {}
    analyst_by_key: dict[str, bool] = {}
    for r in rows:
        name = str(r["name"])
        key = kpi_group_key(name)
        names = obs_by_name.setdefault(key, {})
        names[name] = names.get(name, 0) + int(r["obs"])
        tickers_by_key.setdefault(key, set()).add(str(r["ticker"]))
        if has_origin and str(r["origin"] or "analyst") == "analyst":
            analyst_by_key[key] = True
    prelim: list[tuple[str, int, bool]] = []
    for key, names in obs_by_name.items():
        prelim.append(
            (_kpi_representative(names), len(tickers_by_key[key]), analyst_by_key.get(key, False))
        )
    # Most broadly-reported first, then alphabetical — same shape as the other
    # domains' ORDER BY.
    prelim.sort(key=lambda t: (-t[1], t[0]))
    entries: list[dict[str, object]] = []
    for rep, n_tickers, analyst in prelim[:limit_per_domain]:
        entry: dict[str, object] = {"token": f"kpi:{rep}", "label": rep, "tickers": n_tickers}
        if has_origin:
            entry["origin"] = "analyst" if analyst else "capture"
        entries.append(entry)
    return entries


def _kpi_representative(names: dict[str, int]) -> str:
    """The display name for a de-fragmented KPI group: the SHORTEST variant (the
    cleanest leaf), the most-observations one breaking ties, then alphabetical."""
    return min(names, key=lambda n: (len(n), -names[n], n))


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff PRAGMA reports ``column`` on ``table`` (False on any error)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(str(r["name"]) == column for r in rows)


def _override_token(fact_kind: str, fact_key: str) -> tuple[str, str, str] | None:
    """Map a ``fact_overrides`` (fact_kind, fact_key) to a (domain, token,
    label) for the picker, or None when it has no single pickable metric.

    Every token is :meth:`MetricRef.parse_token`-parseable so a picked
    override-only fact validates like any catalog entry. Record-level segment
    overrides (``fact_key`` with no ``|``) name a whole dimension, not one
    slice — skipped (their cells are already enumerated from the base tables).
    """
    if fact_kind == "financial_fact":
        return ("fin", f"fin:{fact_key}", fact_key)
    if fact_kind == "kpi":
        return ("kpi", f"kpi:{fact_key}", fact_key)
    if fact_kind == "segment":
        parts = fact_key.split("|")
        if len(parts) == 3 and all(parts):
            dt, dn, m = parts
            return ("seg", f"seg:{dt}:{dn}:{m}", f"{dn} {m} ({dt})")
    return None


def _union_override_only(
    conn: sqlite3.Connection,
    out: dict[str, list[dict[str, object]]],
    symbols: list[str],
    marks: str,
    limit_per_domain: int,
) -> None:
    """Add picker entries for facts that exist ONLY as a ``fact_overrides`` row.

    The base queries enumerate the substrate tables; a fact a company published
    that FMP never carried lives only in ``fact_overrides`` and would otherwise
    be invisible (and so unpickable). Union those in, deduped against the base
    tokens already present (an override that supersedes an existing base row is
    a no-op here — its token is already listed). Best-effort: a missing
    ``fact_overrides`` table is a no-op.
    """
    existing = {str(e["token"]) for entries in out.values() for e in entries}
    try:
        rows = conn.execute(
            f"""
            SELECT fact_kind, fact_key, COUNT(DISTINCT ticker) AS n
            FROM fact_overrides
            WHERE ticker IN ({marks}) AND status = 'active' AND action <> 'drop'
            GROUP BY fact_kind, fact_key ORDER BY n DESC, fact_key ASC LIMIT ?
            """,
            (*symbols, limit_per_domain),
        ).fetchall()
    except sqlite3.Error:
        return
    for r in rows:
        mapped = _override_token(str(r["fact_kind"]), str(r["fact_key"]))
        if mapped is None:
            continue
        domain, token, label = mapped
        if token in existing:
            continue
        existing.add(token)
        out[domain].append(
            {"token": token, "label": label, "tickers": int(r["n"]), "override_only": True}
        )


def _catalog_query(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
    to_entry: Callable[[sqlite3.Row], dict[str, object]],
) -> list[dict[str, object]]:
    """Run one catalog query, tolerating a missing table ([] instead)."""
    try:
        return [to_entry(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []

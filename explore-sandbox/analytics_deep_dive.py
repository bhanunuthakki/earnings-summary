"""The analytics deep-dive sandbox: deterministic catalog → ViewSpec.

A read-only Streamlit surface over the SAME ViewSpec engine the cockpit and
reports use. Pick an explicit database (the synthetic benchmark clone, or any
restored snapshot path), pick tickers, browse the de-fragmented metric
catalog, compose a view (metrics × tickers × cadence × periods × transform),
and read the resolved cells with units, warnings, and metric definitions.

What this is NOT: no LLM anywhere, no writes, no production authority. The
database path is always explicit — this surface never infers a checkout-local
or production database — and every number comes from the provenance-aware
resolver the rest of the app shares.

Optional-dependency surface: `pip install -e .[sandbox]`, then
`streamlit run explore-sandbox/analytics_deep_dive.py`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

SANDBOX_DIR = Path(__file__).resolve().parent
REPO_ROOT = SANDBOX_DIR.parent
for _entry in (str(REPO_ROOT / "src"), str(SANDBOX_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import sandbox_kit  # noqa: E402

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from ui.tokens import page_title  # noqa: E402
from viewspec.engine import execute_view, metric_catalog  # noqa: E402
from viewspec.spec import (  # noqa: E402
    CADENCES,
    MAX_CAGR_YEARS,
    MAX_METRICS,
    MAX_PERIODS,
    MAX_TICKERS,
    TRANSFORMS,
    ViewSpec,
    ViewSpecError,
)

_SYNTHETIC_CLONE = REPO_ROOT / ".tmp" / "vs_opt.db"


def _resolve_database(selected: str, typed: str) -> Path | None:
    """The explicit database to read, or None with the reason already shown.

    Never inferred: the operator names the synthetic clone or a restored
    snapshot. A checkout-local ``data/portfolio.db`` is refused outright —
    it is an invalid artifact on a Mac checkout, not a fallback.
    """
    raw = typed.strip() or (selected if not selected.startswith("(") else "")
    if not raw:
        st.info("Name a database to explore: the synthetic clone, or a restored snapshot path.")
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path = path.resolve()
    if path == (REPO_ROOT / "data" / "portfolio.db").resolve():
        st.error(
            "checkout-local data/portfolio.db is never a valid analytics database; "
            "use a restored snapshot or the synthetic clone"
        )
        return None
    if not path.exists():
        st.error(f"database not found: {path}")
        return None
    return path


def _available_tickers(db_path: Path) -> list[str]:
    """Distinct tickers the chosen database can actually chart.

    Fact tables first — a ticker with definitions but no admitted facts
    (the synthetic clone carries real-ticker definition residue) produces an
    empty catalog, so the picker lists data-bearing tickers before
    definition-only ones.
    """
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        for table in ("kpi_facts", "financial_facts", "kpi_definitions", "documents"):
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT ticker FROM {table} ORDER BY ticker"  # nosec B608
                ).fetchall()
            except sqlite3.Error:
                continue
            tickers = [str(row[0]) for row in rows if row[0]]
            if tickers:
                return tickers[:200]
        return []
    finally:
        conn.close()


def _catalog_options(catalog: dict[str, list[dict[str, object]]]) -> dict[str, str]:
    """Multiselect display label → catalog token, domain-grouped."""
    label_to_token: dict[str, str] = {}
    for domain in ("fin", "kpi", "seg", "detail"):
        for entry in catalog.get(domain, []):
            label = f"[{domain}] {entry['label']} · {entry['tickers']} tickers"
            label_to_token[label] = str(entry["token"])
    return label_to_token


st.set_page_config(page_title=page_title("explore-sandbox", "analytics deep-dive"), layout="wide")
st.markdown(f"<style>{sandbox_kit.theme_css()}</style>", unsafe_allow_html=True)
st.caption(
    "Deterministic and read-only: no LLM, no writes — every cell comes from the "
    "same provenance-aware resolver as the cockpit and reports."
)

with st.sidebar:
    st.markdown(sandbox_kit.panel_section_title("Database"), unsafe_allow_html=True)
    choices = [str(_SYNTHETIC_CLONE)] if _SYNTHETIC_CLONE.exists() else []
    selected_db = st.selectbox(
        "database",
        choices if choices else ["(name a database below)"],
        help="Explicit databases only: the synthetic benchmark clone, or a path you type.",
    )
    typed_db = st.text_input(
        "explicit database path",
        "",
        help="A restored snapshot or synthetic clone. Never data/portfolio.db.",
    )

db_path = _resolve_database(selected_db, typed_db)
if db_path is None:
    st.stop()

tickers_all = _available_tickers(db_path)
with st.sidebar:
    st.markdown(sandbox_kit.panel_section_title("Tickers"), unsafe_allow_html=True)
    if not tickers_all:
        st.caption("this database lists no tickers")
    selected_tickers = st.multiselect(
        "tickers",
        tickers_all,
        default=tickers_all[:4],
        max_selections=MAX_TICKERS,
    )
if not selected_tickers:
    st.info("Pick at least one ticker to browse its metric catalog.")
    st.stop()

catalog = metric_catalog(db_path, list(selected_tickers))
label_to_token = _catalog_options(catalog)
if not label_to_token:
    st.markdown(
        sandbox_kit.k_empty("No admitted metrics for these tickers"),
        unsafe_allow_html=True,
    )
    st.stop()

st.markdown(sandbox_kit.panel_section_title("Compose a view"), unsafe_allow_html=True)
selected_labels = st.multiselect(
    "metrics",
    list(label_to_token),
    max_selections=MAX_METRICS,
)
if not selected_labels:
    st.info("Pick one or more metrics from the catalog.")
    st.stop()

transform_col, cadence_col, periods_col = st.columns(3)
with transform_col:
    transform = st.selectbox("transform", TRANSFORMS)
with cadence_col:
    cadence = st.selectbox("cadence", CADENCES)
with periods_col:
    periods = st.slider("periods", min_value=1, max_value=MAX_PERIODS, value=12)
if transform == "cagr":
    cagr_years = st.slider("cagr lookback (years)", min_value=1, max_value=MAX_CAGR_YEARS, value=3)
else:
    cagr_years = 3

spec_dict: dict[str, object] = {
    "tickers": list(selected_tickers),
    "metrics": [label_to_token[label] for label in selected_labels],
    "transform": transform,
    "cadence": cadence,
    "periods": periods,
}
if transform == "cagr":
    spec_dict["cagr_years"] = cagr_years
try:
    spec = ViewSpec.from_dict(spec_dict)
except ViewSpecError as exc:
    st.error(f"invalid spec: {exc}")
    st.stop()

result = execute_view(spec, db_path=db_path)
for warning in result.warnings:
    st.caption(warning)

if not result.rows:
    st.markdown(sandbox_kit.k_empty("No resolved cells for this spec"), unsafe_allow_html=True)
    st.stop()

for metric in spec.metrics:
    metric_rows = [row for row in result.rows if row.metric == metric]
    if not metric_rows:
        continue
    unit = next((row.unit for row in metric_rows if row.unit), None)
    title = metric.label + (f" ({unit})" if unit else "")
    st.markdown(sandbox_kit.panel_section_title(title), unsafe_allow_html=True)
    frame = pd.DataFrame(
        {row.ticker: [cell.value for cell in row.cells] for row in metric_rows},
        index=result.period_labels,
    )
    st.line_chart(frame)
    st.dataframe(frame, width="stretch")

with st.expander("Metric definitions"):
    for token, definition in result.definitions.items():
        st.caption(f"{token} — {definition}")

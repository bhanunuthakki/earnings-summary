"""Generate a ticker's IR-spreadsheet parser config with an LLM.

Every issuer lays its "historical data" spreadsheet out differently (tab names,
row labels, decimal-vs-percent units), so the `SheetKpi` map is per-company. This
turns that into a pipeline step rather than hand-authored config: dump the
spreadsheet's structure (sheets + data-row labels + sample values), ask the LLM
to map each of the ticker's *canonical* KPIs (its holdings `tier_1_kpis`) to the
row that holds it, and persist the resulting `IrConfig` to
``micro_thesis/ir_config/<T>.json``.

Mapping which row is which KPI — and whether a percent is stored as a decimal —
is exactly an LLM task (it's how the NU config was first written by hand).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import openpyxl

from ir_pipeline.config import IrConfig, SheetKpi, save_config
from ir_pipeline.spreadsheet import _header_row

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# Standard financials line items — these flow from FMP/SEC at higher tiers, so
# they must NOT be sourced from the IR spreadsheet (that would create stray,
# wrong-unit duplicate KPI rows). Matches financials._LINE_ITEM_SPECS.
_FMP_LINE_ITEMS = frozenset(
    {
        "revenue",
        "gross profit",
        "operating income",
        "net income",
        "eps (diluted)",
        "operating cash flow",
        "free cash flow",
        "capex",
    }
)


def _target_kpi_names(ticker: str, repo_root: Path) -> list[str]:
    """Canonical KPI names to map to — the union of the ticker's holdings
    `tier_1_kpis` and `chart_priorities`, minus standard FMP line items.

    `chart_priorities` carries the granular series the report charts (e.g. NU's
    "NPL 15-90d" / "NPL 90d+"), which the spreadsheet stores as distinct rows;
    `tier_1_kpis` sometimes only has a combined form ("NPL 15d+ total") with no
    single row. Standard financials line items (Revenue, Net income, …) are
    excluded — they come from FMP, not IR spreadsheets. The LLM omits any
    remaining target the spreadsheet doesn't carry.
    """
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return []
    holdings = json.loads(path.read_text(encoding="utf-8"))
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        name = name.strip()
        if name and name not in seen and name.lower() not in _FMP_LINE_ITEMS:
            seen.add(name)
            out.append(name)

    for k in holdings.get("tier_1_kpis") or []:
        if isinstance(k, dict):
            _add(str(k.get("name", "")))
    for name in holdings.get("chart_priorities") or []:
        if isinstance(name, str):
            _add(name)
    return out


def dump_sheet_structure(xlsx_path: Path, label_col: int = 1, max_rows_per_sheet: int = 60) -> str:
    """Compact text view of each data sheet: labels + two recent sample values.

    Only sheets with a detectable quarter-date header are included; the samples
    let the model infer unit/scale (a 0.095 cell is a decimal percent → scale 100).
    """
    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True, read_only=True)
    blocks: list[str] = []
    for sheet in wb.sheetnames:
        rows = cast(
            "list[tuple[object, ...]]",
            [tuple(r) for r in wb[sheet].iter_rows(values_only=True)],
        )
        _, col_map = _header_row(rows)
        if not col_map:
            continue
        recent = sorted(col_map)[-2:]
        recent_labels = [col_map[c].date().isoformat() for c in recent]
        lines = [f"## Sheet: {sheet!r}  (quarters across columns; latest = {recent_labels})"]
        for r in rows:
            lab = r[label_col] if label_col < len(r) else None
            if not (isinstance(lab, str) and lab.strip()):
                continue
            samples = [r[c] for c in recent if c < len(r)]
            if any(isinstance(s, (int, float)) and not isinstance(s, bool) for s in samples):
                vals = [round(s, 4) if isinstance(s, float) else s for s in samples]
                lines.append(f"  - {lab.strip()[:80]}  e.g. {vals}")
            if len(lines) > max_rows_per_sheet:
                lines.append("  - … (truncated)")
                break
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    wb.close()
    return "\n\n".join(blocks)


def _build_prompt(ticker: str, structure: str, kpi_names: list[str]) -> str:
    kpi_block = "\n".join(f"- {n}" for n in kpi_names)
    return f"""You are configuring a parser for {ticker}'s investor-relations "historical data" \
spreadsheet. Below is its structure: each data sheet, with its rows (label + two \
recent sample values; quarters run across columns).

For EACH canonical KPI below, find the single spreadsheet row holding its quarterly \
time series:
{kpi_block}

Return ONLY a JSON object keyed by the EXACT canonical KPI name:
  {{"<kpi name>": {{"sheet": "<exact sheet title>", "row_label": "<unique substring of the row label>", "unit": "percent|actual|usd|count|ratio", "scale": 1 or 100}}}}

Rules:
- row_label: a short, case-insensitive SUBSTRING that uniquely identifies the row.
- scale: 100 ONLY if the row stores a percent as a decimal (sample like 0.095 = 9.5%); otherwise 1.
- unit: "percent" for margins / ratios / NPLs, "usd" or "actual" for dollar figures, "count" for customer counts.
- OMIT any KPI that is not present in the spreadsheet (e.g. ROE or CET1 are often not tabulated). Do NOT guess or invent a row.

Spreadsheet structure:
\"\"\"
{structure}
\"\"\"

Return ONLY the JSON object — no prose, no markdown fence."""


def _llm_map(ticker: str, structure: str, kpi_names: list[str]) -> dict[str, dict[str, object]]:
    """Single LLM call → {kpi_name: {sheet, row_label, unit, scale}}."""
    from llm_client import (  # lazy: heavy import chain
        FAST_CLASSIFIER_MODEL,
        JSON_FENCE_RE,
        _call_claude,
    )

    # One-time per-ticker onboarding step; mapping a full sheet structure takes
    # the model ~1-3 min, so allow generous headroom over the default timeout.
    raw = _call_claude(
        _build_prompt(ticker, structure, kpi_names),
        model=FAST_CLASSIFIER_MODEL,
        timeout_seconds=300,
    ).strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    start = raw.find("{")
    if start < 0:
        return {}
    parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(parsed, dict):
        return {}
    return {str(k): v for k, v in parsed.items() if isinstance(v, dict)}


def build_ir_config(
    ticker: str,
    xlsx_path: Path,
    *,
    platform: str,
    results_center_url: str,
    repo_root: Path | None = None,
    label_col: int = 1,
    persist: bool = True,
) -> IrConfig:
    """Map `xlsx_path`'s rows to `ticker`'s canonical KPIs via LLM; build + persist."""
    root = repo_root or _PROJECT_ROOT
    kpi_names = _target_kpi_names(ticker, root)
    structure = dump_sheet_structure(xlsx_path, label_col=label_col)
    mapping = _llm_map(ticker, structure, kpi_names) if (kpi_names and structure) else {}

    kpis: list[SheetKpi] = []
    for name in kpi_names:  # preserve holdings order; only keep mapped KPIs
        spec = mapping.get(name)
        if not isinstance(spec, dict) or not spec.get("sheet") or not spec.get("row_label"):
            continue
        raw_scale = spec.get("scale", 1.0)
        scale = (
            float(cast("float", raw_scale))
            if isinstance(raw_scale, (int, float)) and not isinstance(raw_scale, bool)
            else 1.0
        )
        kpis.append(
            SheetKpi(
                kpi_name=name,
                sheet=str(spec["sheet"]),
                row_label=str(spec["row_label"]),
                unit=str(spec.get("unit", "actual")),
                scale=scale,
            )
        )

    cfg = IrConfig(
        ticker=ticker.upper(),
        platform=platform,
        results_center_url=results_center_url,
        spreadsheet_kpis=tuple(kpis),
        label_col=label_col,
    )
    if persist:
        save_config(cfg, root)
    return cfg

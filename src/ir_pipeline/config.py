"""Per-ticker IR-document configuration for the spreadsheet KPI pipeline.

Each ticker's investor-relations site publishes a quarterly "historical data"
spreadsheet with clean numeric KPI series (one column per quarter). Unlike the
LLM-extracted brief/press-release values, these are the company's own audited
figures — so they ingest at IR_DOC tier and supersede the brief values.

A ticker's `IrConfig` declares:
  * which discovery `platform` adapter finds its current document URLs
    (MZ/mziq for NU and most LatAm issuers; q4cdn for US large-caps), and
  * where each canonical KPI lives in the spreadsheet (`SheetKpi`).

`results_center_url` is the JS-rendered IR page the discovery adapter loads to
resolve the (hash-keyed, quarter-rotating) document URLs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SheetKpi:
    """Where one KPI lives in an IR historical-data spreadsheet.

    `kpi_name` MUST be the canonical `kpi_definitions.name` (the long
    `tier_1_kpis` form, e.g. "Monthly ARPAC (USD)") so the value lands on the
    same definition the report charts read — never a short alias, which would
    fragment into a duplicate definition.
    """

    kpi_name: str
    sheet: str  # worksheet title (exact)
    row_label: str  # case-insensitive substring match on the row's label cell
    unit: str  # "percent" | "actual" | "usd" | "ratio" | "count"
    scale: float = 1.0  # multiply the raw cell (e.g. 100 for a decimal-percent row)


@dataclass(frozen=True)
class IrConfig:
    ticker: str
    platform: str  # discovery adapter key: "mz" | "q4cdn"
    results_center_url: str
    spreadsheet_kpis: tuple[SheetKpi, ...] = ()
    label_col: int = 1  # 0-based column holding the row labels in this issuer's sheets


_CONFIGS: dict[str, IrConfig] = {}


def register(cfg: IrConfig) -> None:
    _CONFIGS[cfg.ticker.upper()] = cfg


def get_config(ticker: str) -> IrConfig | None:
    return _CONFIGS.get(ticker.upper())


def configured_tickers() -> list[str]:
    return sorted(_CONFIGS)


# ---------------------------------------------------------------------------
# Nu Holdings (NU) — MZ/mziq-hosted. Spreadsheet: "Nu Holdings Historical Data".
# Columns are calendar quarters; KPI rows verified against the 1Q26 file.
# Note: ROE and CET1 are NOT tabulated in this spreadsheet — they stay
# press-release-sourced. Risk-adjusted NIM / NPL rows are stored as decimals
# (0.095 = 9.5%), hence scale=100.
# ---------------------------------------------------------------------------
register(
    IrConfig(
        ticker="NU",
        platform="mz",
        results_center_url="https://www.investidores.nu/en/financials/results-center/",
        spreadsheet_kpis=(
            SheetKpi(
                "Monthly ARPAC (USD)",
                "Managerial indicators",
                "Average Revenue per Active Customer",
                "actual",
                1.0,
            ),
            SheetKpi(
                "Risk-adjusted NIM (NIM minus cost of risk)",
                "Managerial indicators",
                "Risk-adjusted NIM (%)",
                "percent",
                100.0,
            ),
            SheetKpi("NPL 15-90d", "NPLs", "15-90 days NPL", "percent", 100.0),
            SheetKpi("NPL 90d+", "NPLs", "90+ NPL", "percent", 100.0),
        ),
    )
)

"""execution/get_portfolio_risk_matrix.py
---------------------------------------
Single-purpose CLI returning the cross-asset correlation matrix, crowding clusters,
factor exposures, and style loadings for the current active portfolio holdings.

Usage:
    python execution/get_portfolio_risk_matrix.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from db_paths import configured_db_path  # noqa: E402
from portfolio_correlation import build_holdings_correlation_from_disk  # noqa: E402
from portfolio_style_factors import build_style_rollup_from_disk  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


class ClusterData(BaseModel):
    model_config = ConfigDict(frozen=True)

    tickers: list[str]
    combined_weight: float | None = None
    avg_intra_corr: float | None = None


class PortfolioRiskMatrixResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    tickers: list[str] = Field(default_factory=list)
    matrix: list[list[float]] = Field(default_factory=lambda: list[list[float]]())
    clusters: list[ClusterData] = Field(default_factory=lambda: list[ClusterData]())
    avg_pairwise_corr: float | None = None
    n_obs: int = 0
    dropped: dict[str, str] = Field(default_factory=dict)
    style_factors: dict[str, Any] = Field(default_factory=dict)
    as_of: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def load_portfolio_risk_matrix(repo_root: Path) -> PortfolioRiskMatrixResponse:
    db_path = configured_db_path(repo_root)
    tickers: list[str] = []

    if db_path.exists():
        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
            cursor = conn.execute(
                "SELECT DISTINCT UPPER(ticker) AS ticker FROM portfolio_holdings WHERE is_active = 1"
            )
            tickers = [str(r[0]) for r in cursor.fetchall()]
            conn.close()
        except sqlite3.Error:
            pass

    if not tickers:
        # Fallback to general monitored universe if holdings empty in test DB
        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
            cursor = conn.execute("SELECT DISTINCT UPPER(ticker) AS ticker FROM dcf_runs LIMIT 10")
            tickers = [str(r[0]) for r in cursor.fetchall()]
            conn.close()
        except sqlite3.Error:
            pass

    corr_read = (
        build_holdings_correlation_from_disk(repo_root, tickers) if len(tickers) >= 2 else None
    )
    style_read = build_style_rollup_from_disk(repo_root, tickers) if len(tickers) >= 1 else None

    clusters: list[ClusterData] = []
    if corr_read and corr_read.clusters:
        for c in corr_read.clusters:
            clusters.append(
                ClusterData(
                    tickers=list(c.tickers),
                    combined_weight=c.combined_weight_pct,
                    avg_intra_corr=c.avg_corr,
                )
            )

    style_factors: dict[str, Any] = {}
    if style_read and style_read.legs:
        for leg in style_read.legs:
            style_factors[leg.key] = {
                "label": leg.label,
                "spread_label": leg.spread_label,
                "book_loading": leg.book_beta,
                "names_priced": leg.names_priced,
            }

    if corr_read:
        return PortfolioRiskMatrixResponse(
            tickers=list(corr_read.tickers),
            matrix=corr_read.matrix,
            clusters=clusters,
            avg_pairwise_corr=corr_read.avg_pairwise_corr,
            n_obs=corr_read.n_obs,
            dropped=corr_read.dropped,
            style_factors=style_factors,
        )

    return PortfolioRiskMatrixResponse(
        tickers=tickers,
        style_factors=style_factors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    response = load_portfolio_risk_matrix(args.repo_root.resolve())
    print(response.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

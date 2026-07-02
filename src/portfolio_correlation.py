"""Holdings pairwise correlation matrix + crowding clusters.

The tracker's positioning endpoint reports each name's correlation TO SPY, so
the book's crowding read has been one-dimensional: "how much does everything
co-move with the index". What it can't answer is which HOLDINGS move as one —
"these N names are effectively one bet" — because the tracker exposes no
per-position daily series. The FMP price-chart cache does (the same substrate
the next-dollar covariance reads via ``allocation.price_history``), so this
module derives the pairwise read client-side:

* the NxN sample correlation matrix over the holdings' latest common daily
  log-return window (``build_aligned_returns`` — 252-obs lookback, 120-obs
  floor, thin-history names dropped with a reason, never silently);
* CROWDING CLUSTERS: connected components of the "pairwise corr >= 0.70"
  graph (single-linkage — if A~B and B~C cluster, A joins even at lower
  A-C corr, matching how a de-risking trim actually propagates), each with
  its combined book weight and average intra-cluster correlation.

Pure disk reads + numpy — never the network, never the DB — so the Risk
panel section renders with the tracker down (weights degrade to the
materialized cache exactly like the style-factor section).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from allocation.price_history import (
    build_aligned_returns,
    daily_log_returns,
    load_daily_closes,
)

__all__ = [
    "CLUSTER_CORR",
    "CorrelationRead",
    "CrowdingCluster",
    "build_holdings_correlation_from_disk",
    "correlation_matrix",
    "find_crowding_clusters",
]

# Two names whose daily returns correlate at/above this are treated as one
# bet for clustering. 0.70 ~ half the variance shared — coarse by design
# (the clusters rank attention, they don't measure a hedge ratio).
CLUSTER_CORR = 0.70


@dataclass(slots=True)
class CrowdingCluster:
    """One group of holdings that co-move enough to be a single bet."""

    tickers: list[str]  # sorted, >= 2 names
    combined_weight_pct: float  # sum of the members' fraction-of-book, in percent
    avg_corr: float  # mean pairwise correlation inside the cluster
    min_corr: float  # weakest intra-cluster link (single-linkage honesty)


@dataclass(slots=True)
class CorrelationRead:
    """The pairwise matrix + clusters, with the provenance to trust them."""

    tickers: list[str]  # matrix order (sorted)
    matrix: list[list[float]]  # NxN, symmetric, 1.0 diagonal
    clusters: list[CrowdingCluster] = field(default_factory=list[CrowdingCluster])
    n_obs: int = 0
    prices_through: date | None = None
    avg_pairwise_corr: float | None = None  # book-level mean of the off-diagonal
    dropped: dict[str, str] = field(default_factory=dict[str, str])  # ticker -> reason


def correlation_matrix(matrix: np.ndarray) -> np.ndarray | None:
    """Sample correlation over a TxN daily-return matrix (rows = days).

    Returns ``None`` when any column is degenerate (zero variance) — the
    caller should have dropped it — or the panel is too thin to correlate.
    """
    if matrix.ndim != 2 or matrix.shape[0] < 3 or matrix.shape[1] < 2:
        return None
    stds = matrix.std(axis=0)
    if not bool(np.all(stds > 0.0)) or not bool(np.all(np.isfinite(stds))):
        return None
    corr = np.corrcoef(matrix, rowvar=False)
    if not bool(np.all(np.isfinite(corr))):
        return None
    return np.clip(corr, -1.0, 1.0)


def find_crowding_clusters(
    tickers: Sequence[str],
    corr: np.ndarray,
    weights: Mapping[str, float] | None = None,
    *,
    threshold: float = CLUSTER_CORR,
) -> list[CrowdingCluster]:
    """Connected components of the "corr >= threshold" graph, largest combined
    weight first. ``weights`` is ticker -> fraction-of-book (absent names count
    0, so a cluster of unheld names still surfaces but ranks last)."""
    n = len(tickers)
    weights = weights or {}
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if float(corr[i, j]) >= threshold:
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: list[CrowdingCluster] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        pair_corrs = [float(corr[i, j]) for k, i in enumerate(members) for j in members[k + 1 :]]
        names = sorted(tickers[i] for i in members)
        clusters.append(
            CrowdingCluster(
                tickers=names,
                combined_weight_pct=100.0 * sum(weights.get(t, 0.0) for t in names),
                avg_corr=sum(pair_corrs) / len(pair_corrs),
                min_corr=min(pair_corrs),
            )
        )
    clusters.sort(key=lambda c: (-c.combined_weight_pct, -c.avg_corr, c.tickers[0]))
    return clusters


def build_holdings_correlation_from_disk(
    repo_root: Path,
    tickers: Sequence[str],
    weights: Mapping[str, float] | None = None,
    *,
    threshold: float = CLUSTER_CORR,
) -> CorrelationRead | None:
    """Assemble the pairwise read from the FMP price-chart cache.

    Returns ``None`` when fewer than two holdings have enough aligned history
    (nothing pairwise to say — the caller renders the empty state). Names with
    thin/absent history are reported in ``dropped``, never silently omitted.
    """
    returns_by_ticker: dict[str, dict[date, float]] = {}
    missing: dict[str, str] = {}
    for t in {t.upper() for t in tickers}:
        rets = daily_log_returns(load_daily_closes(t, repo_root))
        if rets:
            returns_by_ticker[t] = rets
        else:
            missing[t] = "no daily price history on file"
    aligned = build_aligned_returns(returns_by_ticker)
    if aligned is None:
        return None
    # Degenerate (zero-variance) columns would poison corrcoef — drop them.
    matrix = aligned.matrix
    keep = [i for i, s in enumerate(matrix.std(axis=0)) if float(s) > 0.0 and math.isfinite(s)]
    dropped = dict(missing)
    dropped.update(aligned.dropped)
    if len(keep) < len(aligned.tickers):
        for i, t in enumerate(aligned.tickers):
            if i not in keep:
                dropped[t] = "constant return series over the window"
        matrix = matrix[:, keep]
    kept_tickers = [aligned.tickers[i] for i in keep]
    corr = correlation_matrix(matrix)
    if corr is None or len(kept_tickers) < 2:
        return None
    off_diag = [
        float(corr[i, j]) for i in range(len(kept_tickers)) for j in range(i + 1, len(kept_tickers))
    ]
    return CorrelationRead(
        tickers=kept_tickers,
        matrix=[
            [float(corr[i, j]) for j in range(len(kept_tickers))] for i in range(len(kept_tickers))
        ],
        clusters=find_crowding_clusters(kept_tickers, corr, weights, threshold=threshold),
        n_obs=len(aligned.dates),
        prices_through=aligned.dates[-1] if aligned.dates else None,
        avg_pairwise_corr=sum(off_diag) / len(off_diag) if off_diag else None,
        dropped=dropped,
    )

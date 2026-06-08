"""Resolve a requested KPI label to the canonical ``kpi_definitions.name``.

Shared by every consumer that joins a human-supplied KPI label — a holdings
``chart_priorities`` entry, a ``break_rules`` ``kpi_name``, a tier-N ledger name
— to the stored ``kpi_definitions`` row. KPI names fragment over time: the
issuer's IR spreadsheet ingests "Monthly ARPAC (USD)" while an older LLM brief
stored a bare "Monthly ARPAC", leaving two definitions for the same metric where
one is fully populated and the other near-empty.

Exact-name matching picks whichever spelling the label happens to use, so a
short holdings label can resolve to the sparse duplicate and the consumer then
charts / evaluates an almost-empty series. This module instead matches on a
parenthetical-insensitive normalized name and prefers the definition carrying
the MOST observations, so a fragmented duplicate can never shadow the canonical
series.

Originally ``financials._resolve_kpi_definition_name`` / ``_normalize_kpi_name``
(PR #195); extracted here so the §3 chart loader, the §2 break-rule ledger, and
the break-rule evaluator all share one resolver instead of three exact-match
lookups that drift apart.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

# kpi_facts.fiscal_period_type values that denote a quarterly observation. The
# §3 chart cadence is quarterly, so the chart loader measures richness over
# these buckets only; the break-rule paths query every period type and pass
# period_types=None so "most observations" is measured over the rows they read.
QUARTERLY_FACT_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")

# kpi_facts.fiscal_period_type values that denote an annual (fiscal-year-end)
# observation — the cadence-aware twin of QUARTERLY_FACT_PERIOD_TYPES. Matches the
# financials annual set (report.sections._common.ANNUAL_PERIOD_TYPES) so an annual
# KPI series aligns with the annual line-item axis. Consumers select these rows
# when a definition's reporting_cadence is 'annual' (see reporting_cadence_for).
ANNUAL_FACT_PERIOD_TYPES: tuple[str, ...] = ("FY", "annual")

# Trailing "(...)" qualifier on a stored KPI name, e.g. "Monthly ARPAC (USD)" or
# "ROE (annualized, consolidated)". Stripped when matching a requested label to a
# stored definition.
_KPI_NAME_PAREN_TAIL_RX = re.compile(r"\s*\([^()]*\)\s*$")


def normalize_kpi_name(name: str) -> str:
    """Lowercase, collapse whitespace, drop trailing parenthetical qualifiers.

    "Monthly ARPAC (USD)" and "Monthly ARPAC" both normalize to "monthly arpac"
    so a short label still resolves to the canonical definition. Only *trailing*
    parentheticals are stripped; an interior qualifier is kept so genuinely
    distinct metrics ("NIM" vs "Risk-adjusted NIM (...)") don't collide.
    """
    s = name.strip()
    while True:
        stripped = _KPI_NAME_PAREN_TAIL_RX.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    return " ".join(s.split()).lower()


def resolve_kpi_definition_name(
    conn: sqlite3.Connection,
    ticker: str,
    requested: str,
    *,
    period_types: Sequence[str] | None = None,
) -> str | None:
    """Choose which stored ``kpi_definitions.name`` to use for a requested label.

    Among this ticker's definitions that carry facts (optionally restricted to
    ``period_types``), accept an exact name match or a normalized-equal
    (parenthetical-insensitive) one, then pick the candidate with the MOST
    observations — exactness only breaks ties. This keeps a near-empty
    fragmented duplicate (e.g. a stray "Monthly ARPAC" with 2 rows) from
    shadowing the fully-populated canonical "Monthly ARPAC (USD)".

    ``period_types`` filters the observation COUNT to those fiscal_period_type
    buckets so richness is measured over the rows the caller will actually read:
    the quarterly chart loader passes ``QUARTERLY_FACT_PERIOD_TYPES``; the
    break-rule paths (which query every period type) pass None. Returns None when
    no stored definition — among those carrying facts — matches the label.

    Requires ``conn.row_factory = sqlite3.Row`` (every consumer's connection
    already sets it).
    """
    cur = conn.cursor()
    if period_types:
        placeholders = ",".join("?" * len(period_types))
        cur.execute(
            f"""
            SELECT kd.name AS name, COUNT(*) AS n
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
              AND kf.fiscal_period_type IN ({placeholders})
            GROUP BY kd.name
            """,
            (ticker.upper(), *period_types),
        )
    else:
        cur.execute(
            """
            SELECT kd.name AS name, COUNT(*) AS n
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
            GROUP BY kd.name
            """,
            (ticker.upper(),),
        )
    want = normalize_kpi_name(requested)
    best_name: str | None = None
    best_rank: tuple[int, int] = (-1, -1)  # (obs_count, exactness)
    for r in cur.fetchall():
        stored = str(r["name"])
        if stored == requested:
            exactness = 1
        elif normalize_kpi_name(stored) == want:
            exactness = 0
        else:
            continue
        rank = (int(r["n"]), exactness)
        if rank > best_rank:
            best_rank = rank
            best_name = stored
    return best_name


def reporting_cadence_for(conn: sqlite3.Connection, ticker: str, requested: str) -> str:
    """Return the reporting_cadence ('quarterly' | 'annual' | 'ttm') for the
    definition best matching ``requested``, defaulting to ``'quarterly'``.

    Matching is *fact-independent* (a tracked-but-empty annual KPI still
    resolves): exact name, then a parenthetical-insensitive normalized name. When
    several definitions normalize-match, an ``'annual'`` marker wins over
    ``'quarterly'`` so a sparse fragmented duplicate can't mask the annual
    cadence. Defensive: returns ``'quarterly'`` when the column is absent
    (pre-0072 / minimal test DBs) or no definition matches — so every caller can
    treat the result as authoritative without re-checking the schema.

    Requires ``conn.row_factory = sqlite3.Row`` (every consumer sets it).
    """
    cols = {c["name"] for c in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()}
    if "reporting_cadence" not in cols:
        return "quarterly"
    want = normalize_kpi_name(requested)
    best = "quarterly"
    found = False
    for row in conn.execute(
        "SELECT name, reporting_cadence FROM kpi_definitions WHERE ticker = ?",
        (ticker.upper(),),
    ):
        stored = str(row["name"])
        if stored == requested or normalize_kpi_name(stored) == want:
            found = True
            cadence = str(row["reporting_cadence"] or "quarterly")
            if cadence == "annual":
                return "annual"
            best = cadence
    return best if found else "quarterly"

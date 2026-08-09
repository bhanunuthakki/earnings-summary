"""§6 Say-Do analysis — pairwise prior-quarter guidance vs current-quarter results.

Sourced from .tmp/SayDo_{TICKER}_Q{prev}_{prev_yr}_Q{curr}_{curr_yr}.txt files
written by execution/build_saydo_pairs.py from per-quarter LLM summaries. Newest first.

Each card is parsed for the LLM's verdict line so the renderer can show a
summary table (rating + thesis view) before the per-quarter breakdown.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from report.models import (
    SayDoCard,
    SayDoHistoricalMetric,
    SayDoSection,
    SectionStatus,
)
from report.sections._common import has_table, missing, open_repo_db

_SAYDO_FILENAME_RX = re.compile(
    r"^SayDo_(?P<t>[A-Z][A-Z0-9.]*)_Q(?P<pq>[1-4])_(?P<py>\d{4})_Q(?P<cq>[1-4])_(?P<cy>\d{4})\.txt$"
)
# LLM puts the rating on a line like "**Performance Rating:** **EXCEEDED**" — match flexibly.
_RATING_RX = re.compile(
    r"Performance Rating:?\s*\**\s*\**\s*(MET|MISSED|EXCEEDED|MIXED)\b",
    re.IGNORECASE,
)
# Capture the rest of the "Thesis View:" / "Attribution:" line until newline.
_THESIS_VIEW_RX = re.compile(
    r"Thesis View:?\s*\**\s*([^\n]+?)(?:\*\*)?\s*$", re.IGNORECASE | re.MULTILINE
)
_ATTRIBUTION_RX = re.compile(
    r"Attribution:?\s*\**\s*([^\n]+?)(?:\*\*)?\s*$", re.IGNORECASE | re.MULTILINE
)
_RatingValue = Literal["MET", "MISSED", "EXCEEDED", "MIXED", "unknown"]


def build(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> SayDoSection:
    tmp_dir = repo_root / ".tmp"
    cards = _scan(tmp_dir, ticker)

    historical_metrics: list[SayDoHistoricalMetric] = []
    db_conn = open_repo_db(repo_root, conn)
    if db_conn:
        try:
            if has_table(db_conn, "saydo_historical_metrics"):
                cur = db_conn.execute(
                    "SELECT id, period_made, period_target, kpi_name, comparator, "
                    "       target_value, realized_value, outcome, guidance_narrative, realized_narrative "
                    "FROM saydo_historical_metrics WHERE ticker = ? "
                    "ORDER BY period_target DESC, period_made DESC, id DESC",
                    (ticker.upper(),),
                )
                for row in cur.fetchall():
                    pm = row["period_made"]
                    pt = row["period_target"]
                    if isinstance(pm, str):
                        pm = datetime.fromisoformat(pm)
                    if isinstance(pt, str):
                        pt = datetime.fromisoformat(pt)

                    realized = (
                        float(row["realized_value"]) if row["realized_value"] is not None else None
                    )
                    historical_metrics.append(
                        SayDoHistoricalMetric(
                            id=int(row["id"]),
                            period_made=pm,
                            period_target=pt,
                            kpi_name=row["kpi_name"],
                            comparator=row["comparator"],
                            target_value=float(row["target_value"]),
                            realized_value=realized,
                            outcome=row["outcome"],
                            guidance_narrative=row["guidance_narrative"],
                            realized_narrative=row["realized_narrative"],
                        )
                    )
        except Exception:
            pass
        finally:
            if conn is None:
                db_conn.close()

    if not cards:
        return SayDoSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(build_saydo_pairs)",
                fix_command=f"python execution/build_saydo_pairs.py --ticker {ticker.upper()}",
                detail="No SayDo_*.txt files found under .tmp/.",
            ),
            historical_metrics=historical_metrics,
        )
    return SayDoSection(status=SectionStatus.OK, cards=cards, historical_metrics=historical_metrics)


def _scan(tmp_dir: Path, ticker: str) -> list[SayDoCard]:
    if not tmp_dir.exists():
        return []
    upper = ticker.upper()
    found: list[SayDoCard] = []
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        m = _SAYDO_FILENAME_RX.match(path.name)
        if not m or m.group("t").upper() != upper:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        rating = _parse_rating(text)
        thesis_view = _parse_thesis_view(text)
        attribution = _parse_attribution(text)
        found.append(
            SayDoCard(
                current_quarter=f"Q{m.group('cq')}",
                current_year=int(m.group("cy")),
                prior_quarter=f"Q{m.group('pq')}",
                prior_year=int(m.group("py")),
                saydo_md=text,
                rating=rating,
                thesis_view=thesis_view,
                attribution=attribution,
            )
        )
    found.sort(key=lambda c: (c.current_year, c.current_quarter), reverse=True)
    return found


def _parse_rating(text: str) -> _RatingValue:
    # 1. Block-based search: Analyst Verdict section
    idx = text.lower().find("### 3. analyst verdict")
    if idx != -1:
        verdict_sec = text[idx:]
        next_sec_idx = verdict_sec.lower().find("###", 10)
        if next_sec_idx != -1:
            verdict_sec = verdict_sec[:next_sec_idx]

        # Find first bolded word MET/MISSED/EXCEEDED/MIXED
        m = re.search(r"\*\*(MET|MISSED|EXCEEDED|MIXED)\*\*", verdict_sec, re.IGNORECASE)
        if m:
            return cast(_RatingValue, m.group(1).upper())
        # Fallback to plain words
        m = re.search(r"\b(MET|MISSED|EXCEEDED|MIXED)\b", verdict_sec, re.IGNORECASE)
        if m:
            return cast(_RatingValue, m.group(1).upper())

    # 2. Try traditional label regex
    m = _RATING_RX.search(text)
    if m:
        return cast(_RatingValue, m.group(1).upper())

    # 3. Global bold word fallback
    m = re.search(r"\*\*(MET|MISSED|EXCEEDED|MIXED)\*\*", text, re.IGNORECASE)
    if m:
        return cast(_RatingValue, m.group(1).upper())

    return "unknown"


def _parse_attribution(text: str) -> str | None:
    # 1. Parse under Adversarial Loop (block-based)
    idx = text.lower().find("### 4. adversarial loop")
    if idx != -1:
        loop_sec = text[idx:]
        next_sec_idx = loop_sec.lower().find("###", 10)
        if next_sec_idx != -1:
            loop_sec = loop_sec[:next_sec_idx]

        # Find Resolution: paragraph
        res_idx = loop_sec.lower().find("**resolution:**")
        if res_idx != -1:
            res_part = loop_sec[res_idx + 15 :].strip()
            p = res_part.split("\n\n")[0].strip()
            p = p.replace("**", "").replace("_", "").strip()
            return re.sub(r"\s+", " ", p)

        # If no Resolution, grab Net Conviction line
        net_conv_idx = loop_sec.lower().find("**net conviction:")
        if net_conv_idx != -1:
            line = loop_sec[net_conv_idx:].split("\n")[0].strip()
            return line.replace("**", "").replace("_", "").strip()

    # 2. Try traditional label (only matching if it doesn't look like a header)
    for line in text.splitlines():
        if line.strip().startswith("###"):
            continue
        m = _ATTRIBUTION_RX.search(line)
        if m:
            extracted = m.group(1).replace("**", "").strip()
            extracted = re.sub(r"\s+", " ", extracted).strip()
            return extracted or None

    return None


def _parse_thesis_view(text: str) -> str | None:
    # 1. Parse under Thesis Impact (block-based)
    idx = text.lower().find("### 5. thesis impact")
    if idx != -1:
        impact_sec = text[idx + 20 :].strip()
        next_sec_idx = impact_sec.lower().find("###")
        if next_sec_idx != -1:
            impact_sec = impact_sec[:next_sec_idx]

        paragraphs = [p.strip() for p in impact_sec.split("\n\n") if p.strip()]
        if paragraphs:
            p = paragraphs[0].replace("**", "").replace("_", "").strip()
            return re.sub(r"\s+", " ", p)

    # 2. Try traditional label (only matching if it doesn't look like a header)
    for line in text.splitlines():
        if line.strip().startswith("###"):
            continue
        m = _THESIS_VIEW_RX.search(line)
        if m:
            extracted = m.group(1).replace("**", "").strip()
            extracted = re.sub(r"\s+", " ", extracted).strip()
            return extracted or None

    return None

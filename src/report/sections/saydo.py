"""§6 Say-Do analysis — pairwise prior-quarter guidance vs current-quarter results.

Sourced from .tmp/SayDo_{TICKER}_Q{prev}_{prev_yr}_Q{curr}_{curr_yr}.txt files
written by execution/build_saydo_pairs.py from per-quarter LLM summaries. Newest first.

The section imports all supported commitment rows and prepares strategic
selection, verdicts and context before render. Full narrative stays available.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from llm_client import (
    compose_anchor_block,
    generate_saydo_filter,
    load_bear_anchor,
    load_ir_anchor,
    load_owner_profile_anchor,
    load_priors_anchor,
    load_thesis_anchor,
    load_worldview_anchor,
)
from report.models import (
    PrintVsGuideRow,
    SayDoCard,
    SayDoHistoricalMetric,
    SayDoSection,
    SayDoSelectionDisposition,
    SayDoVerdictRow,
    SectionStatus,
)
from report.sections._common import has_table, missing, open_repo_db
from report.sections.p3_data import load_saydo_verdicts

log = logging.getLogger(__name__)

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
    enable_llm: bool = False,
    force_refresh: bool = False,
    strict_refresh: bool = False,
) -> SayDoSection:
    tmp_dir = repo_root / ".tmp"
    cards = _scan(tmp_dir, ticker)
    prepare_cards(
        ticker,
        repo_root,
        cards,
        enable_llm=enable_llm,
        force_refresh=force_refresh,
        strict_refresh=strict_refresh,
    )

    historical_metrics: list[SayDoHistoricalMetric] = []
    verdicts: list[SayDoVerdictRow] = []
    verdicts_availability: Literal["present", "empty", "not_configured", "unavailable"] = (
        "not_configured"
    )
    try:
        db_conn = open_repo_db(repo_root, conn)
    except (sqlite3.Error, OSError):
        db_conn = None
        verdicts_availability = "unavailable"
    if db_conn:
        try:
            if has_table(db_conn, "management_commitments"):
                verdicts = load_saydo_verdicts(ticker, conn=db_conn)
                verdicts_availability = "present" if verdicts else "empty"
            else:
                verdicts_availability = "unavailable"
        except (sqlite3.Error, OSError, ValueError, KeyError):
            verdicts_availability = "unavailable"
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

    if verdicts_availability == "unavailable":
        log.warning({"event": "saydo_verdicts_unavailable", "ticker": ticker})
    if not cards:
        return SayDoSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(build_saydo_pairs)",
                fix_command=f"python execution/build_saydo_pairs.py --ticker {ticker.upper()}",
                detail="No SayDo_*.txt files found under .tmp/.",
            ),
            historical_metrics=historical_metrics,
            verdicts=verdicts,
            verdicts_availability=verdicts_availability,
        )
    return SayDoSection(
        status=SectionStatus.OK,
        cards=cards,
        historical_metrics=historical_metrics,
        verdicts=verdicts,
        verdicts_availability=verdicts_availability,
    )


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


def prepare_cards(
    ticker: str,
    repo_root: Path,
    cards: list[SayDoCard],
    *,
    enable_llm: bool = False,
    force_refresh: bool = False,
    strict_refresh: bool = False,
) -> None:
    """Import all supported narrative rows and select the compact view once.

    The narrative remains the source format. Selection stores indices into the
    complete row population, so exports and fuller views retain every row.
    """
    if strict_refresh and not enable_llm:
        raise ValueError("Strict SayDo refresh requires enable_llm")
    for card in cards:
        card.print_vs_guide_rows = parse_print_vs_guide(card)
        card.selected_row_indices = []
        card.selection_disposition = "no_parsable_rows"
    populated = [card for card in cards if card.print_vs_guide_rows]
    if not populated:
        if strict_refresh:
            raise RuntimeError(f"saydo_filter has no parseable commitments for {ticker}")
        return

    anchor_block = ""
    if enable_llm:
        try:
            anchor_block = compose_anchor_block(
                load_thesis_anchor(repo_root, ticker),
                load_bear_anchor(repo_root, ticker),
                load_ir_anchor(repo_root, ticker),
                load_priors_anchor(repo_root, ticker),
                load_worldview_anchor(repo_root),
                load_owner_profile_anchor(repo_root),
            )
        except Exception:
            if strict_refresh:
                raise
            log.warning(
                {
                    "event": "saydo_selection_fallback",
                    "ticker": ticker,
                    "reason": "owner_context_failed",
                }
            )
            for card in populated:
                card.selected_row_indices = list(range(min(8, len(card.print_vs_guide_rows))))
                card.selection_disposition = "failed_fallback"
            return

    for card in populated:
        indices, disposition = _select_print_vs_guide(
            ticker,
            repo_root,
            card,
            anchor_block,
            enable_llm=enable_llm,
            force_refresh=force_refresh,
            strict_refresh=strict_refresh,
        )
        card.selected_row_indices = indices
        card.selection_disposition = disposition


def _select_print_vs_guide(
    ticker: str,
    repo_root: Path,
    card: SayDoCard,
    anchor_block: str,
    *,
    enable_llm: bool,
    force_refresh: bool,
    strict_refresh: bool,
) -> tuple[list[int], SayDoSelectionDisposition]:
    rows = card.print_vs_guide_rows
    fallback = list(range(min(8, len(rows))))
    if not enable_llm:
        return fallback, "non_llm_fallback"
    payload = [
        {
            "id": str(i),
            "metric": row.metric,
            "guide": row.guide,
            "actual": row.actual,
            "verdict": row.verdict,
        }
        for i, row in enumerate(rows)
    ]
    cache_key = _saydo_filter_cache_key(card, payload, anchor_block)
    try:
        cached = None if force_refresh else _load_saydo_filter_cache(repo_root, ticker, cache_key)
        disposition: SayDoSelectionDisposition = "cache"
        if cached is None:
            quarter_label = f"{card.current_quarter} {card.current_year}"
            raw = generate_saydo_filter(ticker, quarter_label, payload, anchor_block=anchor_block)
            cached = _parse_saydo_filter_response(raw)
            disposition = "model"
        selected_ids = set(cached)
        valid_ids = {str(i) for i in range(len(rows))}
        if strict_refresh and (not selected_ids or not selected_ids.issubset(valid_ids)):
            raise ValueError("saydo_filter returned invalid commitment ids")
        if disposition == "model":
            _save_saydo_filter_cache(repo_root, ticker, cache_key, cached)
        selected = [i for i in range(len(rows)) if str(i) in selected_ids]
        if not selected:
            log.warning(
                {
                    "event": "saydo_selection_fallback",
                    "ticker": ticker,
                    "reason": "no_valid_commitment_ids",
                }
            )
            return fallback, "failed_fallback"
        return selected, disposition
    except Exception:
        if strict_refresh:
            raise
        log.warning(
            {"event": "saydo_selection_fallback", "ticker": ticker, "reason": "selection_failed"}
        )
        return fallback, "failed_fallback"


# Header column we accept as the leading column of the print-vs-guide table.
# LLM emits sometimes "Metric", sometimes "Commitment", sometimes "Item" —
# all map to the same row shape (label first, then prior, then current,
# then a verdict-ish trailing column).
_HEADER_LEADERS = ("Metric", "Commitment", "Item", "Promise", "Topic")
_DO_TABLE_HEADER_RX = re.compile(
    r"^\|\s*(?P<leader>" + "|".join(_HEADER_LEADERS) + r")\s*\|(?P<rest>.+)\|\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_TABLE_ROW_RX = re.compile(r"^\|(.+)\|\s*$", re.MULTILINE)


def parse_print_vs_guide(card: SayDoCard) -> list[PrintVsGuideRow]:
    """Extract the most "print-vs-guide"-shaped pipe table from the SayDo md.

    The LLM contract is loose: tables vary across cards
    (Commitment / From Call / Checkable / Gap; Metric / Prior / Current / Direction;
    Item / Guide / Actual / Verdict; etc). We accept any 4+ column pipe table
    whose first header column is one of ``_HEADER_LEADERS`` and map the cells
    to (label, prior-context, current-context, verdict-ish) row by row.
    Returns an empty list on format mismatch. The full narrative remains
    available even when it has no supported table.
    """
    md = card.saydo_md or ""
    if not md:
        return []
    header_match = _DO_TABLE_HEADER_RX.search(md)
    if header_match is None:
        return []
    rest = md[header_match.end() :]
    rows: list[PrintVsGuideRow] = []
    skipped_separator = False
    for row_match in _TABLE_ROW_RX.finditer(rest):
        cells = [c.strip() for c in row_match.group(1).split("|")]
        cells = [c for c in cells if c != ""]
        if not skipped_separator:
            if all(set(c) <= {"-", ":"} for c in cells):
                skipped_separator = True
                continue
            break
        if len(cells) < 3:
            break
        # Permissive mapping: label is always cells[0]; the verdict-like
        # signal is always the LAST cell; "guide" and "actual" share the
        # middle cells (collapse if there's only one).
        label = cells[0]
        verdict_cell = cells[-1]
        if len(cells) >= 4:
            guide = cells[1]
            actual = " · ".join(cells[2:-1])
        else:
            guide = ""
            actual = cells[1]
        verdict = _direction_to_verdict(verdict_cell)
        rows.append(PrintVsGuideRow(metric=label, guide=guide, actual=actual, verdict=verdict))
    return rows


def _saydo_filter_cache_key(
    card: SayDoCard, payload: list[dict[str, str]], anchor_block: str = ""
) -> str:
    h = hashlib.sha256()
    h.update(f"{card.current_quarter}{card.current_year}".encode())
    h.update(b"\x00")
    h.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    h.update(b"\x00")
    h.update(anchor_block.encode("utf-8"))
    return h.hexdigest()


def _saydo_filter_cache_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "data" / "saydo_filter" / f"{ticker.upper()}.json"


def _load_saydo_filter_cache(repo_root: Path, ticker: str, cache_key: str) -> list[str] | None:
    path = _saydo_filter_cache_path(repo_root, ticker)
    if not path.exists():
        return None
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    payload = cast("dict[str, object]", loaded)
    by_key_raw = payload.get("by_key")
    if not isinstance(by_key_raw, dict):
        return None
    by_key = cast("dict[str, object]", by_key_raw)
    entry_raw = by_key.get(cache_key)
    if not isinstance(entry_raw, list):
        return None
    entry = cast("list[object]", entry_raw)
    return [str(i) for i in entry]


def _save_saydo_filter_cache(repo_root: Path, ticker: str, cache_key: str, ids: list[str]) -> None:
    path = _saydo_filter_cache_path(repo_root, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, object] = {"by_key": {}}
    if path.exists():
        try:
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = cast("dict[str, object]", loaded)
                if isinstance(payload.get("by_key"), dict):
                    existing = payload
        except (OSError, json.JSONDecodeError):
            pass
    by_key = cast("dict[str, object]", existing["by_key"])
    by_key[cache_key] = ids
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


_JSON_ARRAY_RX = re.compile(r"\[[^\[\]]*\]", re.DOTALL)


def _parse_saydo_filter_response(raw: str) -> list[str]:
    """LLM sometimes appends a markdown rationale after the JSON array — extract
    just the first top-level array. Tolerant of leading/trailing prose."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Find the first JSON-array-shaped substring and try again.
        m = _JSON_ARRAY_RX.search(raw)
        if m is None:
            raise ValueError("saydo_filter response had no JSON array") from None
        parsed = json.loads(m.group(0))
    if not isinstance(parsed, list):
        raise ValueError("saydo_filter response was not a JSON array")
    return [str(value) for value in cast("list[object]", parsed)]


def _direction_to_verdict(direction: str) -> str:
    """Heuristic mapping of the 'Direction vs. Trajectory' cell to a verdict.

    The LLM uses arrows and free text. We classify by symbol first, then by
    a small keyword set. Unknown directions stay 'unknown' so the renderer
    pill shows the muted/neutral variant.
    """
    text = direction.lower()
    if "↑" in direction or "acceleration" in text or "expansion" in text or "beat" in text:
        return "EXCEEDED"
    if "↓" in direction or "miss" in text or "decel" in text or "contract" in text:
        return "MISSED"
    if "→" in direction or "in-line" in text or "in line" in text or "clean delivery" in text:
        return "MET"
    if "exceeded" in text:
        return "EXCEEDED"
    if "met" in text:
        return "MET"
    return "unknown"

"""§6 Earnings analysis — beat-rate header + per-quarter LLM summaries.

Most recent N quarters render in full; older ones collapse to a 1-paragraph
digest. Pairwise Say-Do lives in §7; full transcripts in §12.

When the `earnings_surprises` table has rows for the ticker, a leading
beat-rate scorecard renders before the per-quarter cards.

When `enable_llm` is True, the builder also runs the 4Q theme split
(``earnings_themes_split`` purpose) that produces two cross-quarter rollups:
what management chose to lead with in prepared remarks vs what analysts
pressed on in Q&A. The split is grounded by the ``transcripts.has_qa_section``
flag (migration 0019) PLUS in-file section markers — see
``_split_transcript_sections`` for the detection ladder.

Sources:
  - .tmp/{TICKER}_{Q}_{YEAR}_summary.txt          per-quarter LLM summary (written by execution/process_ir_documents.py)
  - transcripts/processed/ + transcripts/raw/     path provenance (processed wins on collision)
  - earnings_surprises table                       beat-rate header (FMP primary, yfinance fallback)
  - transcripts table                              has_qa_section tri-state flag
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from llm_client import is_hard_stop
from provenance.selection import selected_transcripts_relation
from report.models import (
    EarningsSection,
    QuarterlyEarningsCard,
    QuoteSnippet,
    SectionStatus,
    SurpriseScorecardCard,
    ThemeRollup,
)
from report.render_clock import render_now
from report.sections._common import budget_gate, missing, open_repo_db
from report.sections._ts_signals import (
    format_signals_as_prompt_block,
    load_signals_for_metrics,
)

# The compute module lives in src/compute/, accessible because src/ is on the
# sys.path (per pyproject.toml `pythonpath = ["src"]`).
_PROJECT_SRC = str(Path(__file__).resolve().parents[2])
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)
from compute.earnings_surprise import surprise_scorecard_for  # noqa: E402

log = logging.getLogger(__name__)

# `_summary.txt` is the canonical per-quarter LLM summary; `_investor_update_summary.txt`
# is the MELI/NU variant (companies that publish investor-update letters in lieu of
# traditional press-release-plus-call). Both feed §5 the same way.
_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)
_TRANSCRIPT_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})\.(?:txt|pdf)$"
)

MAX_CARDS = 8
RECENT_FULL_COUNT = 3  # most recent N quarters get full content in §5

# 4Q rolling theme window. Sized to match the prompt instruction in
# llm_client.extract_qa_vs_prepared_themes; the LLM grounds counts against
# exactly these quarters.
THEMES_LOOKBACK_QUARTERS = 4

THEMES_CACHE_TTL_DAYS = 7

# Aggregator transcripts (fetch_qa_transcript.py) wrap the Q&A in a
# `=== Q&A SEGMENT ===` divider following a metadata preamble. The preamble
# explicitly labels the file as Q&A-only; treat anything matching this
# signature as Q&A in its entirety with no prepared remarks side.
_AGGREGATOR_QA_ONLY_BANNER = re.compile(
    r"=== SYNTHESIZED QUARTERLY UPDATE [^\n]*Q&A SEGMENT ONLY", re.IGNORECASE
)
_AGGREGATOR_QA_SEGMENT_MARKER = re.compile(r"=== Q&A SEGMENT ===", re.IGNORECASE)

# CallStreet / FactSet PDFs use this exact uppercase header at the Q&A
# boundary. Mirrors the detection regex in compute/transcript_ingest.py so
# the splitter agrees with the has_qa_section flag's source of truth.
_CALLSTREET_QA_HEADER = re.compile(r"\n\s*QUESTION\s+AND\s+ANSWER\s+SECTION\b", re.IGNORECASE)

# Operator-introduced first-question phrase. Common in non-CallStreet full
# transcripts (free-form text dumps, some PDF formats) that DO carry prepared
# remarks + Q&A but without an explicit "QUESTION AND ANSWER SECTION" header.
# We use the FIRST occurrence as the Q&A boundary — everything before is
# prepared remarks. Mirrors the analyst-intro regex in
# compute/transcript_ingest.py so the splitter agrees with detect_qa_section.
_OPERATOR_FIRST_QUESTION = re.compile(
    r"first\s+question\s+(?:is\s+from|comes\s+from|will\s+come\s+from)",
    re.IGNORECASE,
)


def build(
    ticker: str,
    repo_root: Path,
    enable_llm: bool = False,
    force_budget_bypass: bool = False,
    conn: sqlite3.Connection | None = None,
) -> EarningsSection:
    """Build the §5 Earnings section.

    ``enable_llm=True`` runs the 4Q theme split extractor on top of the
    standard card assembly. Default off so dev / fresh-checkout builds stay
    cheap and offline-capable (the rest of the section works without LLM).
    """
    tmp_dir = repo_root / ".tmp"
    tr_root = repo_root / "transcripts"

    summaries = _scan_summaries(tmp_dir, ticker)
    transcripts = _scan_transcripts(tr_root, ticker)
    surprise_card = _build_surprise_card(ticker, repo_root, conn=conn)

    if not summaries and not transcripts:
        return EarningsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(process_ir_documents)",
                fix_command=f"python execution/process_ir_documents.py --ticker {ticker.upper()}",
                detail="No per-quarter summaries in .tmp/ and no transcripts in transcripts/{processed,raw}/.",
            ),
            surprise_scorecard=surprise_card,
        )

    # Oldest → newest first, then take last MAX_CARDS, then reverse for display.
    keys_old_to_new = sorted(
        set(summaries.keys()) | set(transcripts.keys()), key=lambda k: (k[1], k[0])
    )[-MAX_CARDS:]
    cards_old_to_new = [_make_card(q, y, summaries, transcripts) for q, y in keys_old_to_new]

    full_old_to_new = cards_old_to_new[-RECENT_FULL_COUNT:]
    digest_old_to_new = cards_old_to_new[:-RECENT_FULL_COUNT]
    for c in full_old_to_new:
        c.is_recent = True

    has_any_llm = any(c.summary_md for c in cards_old_to_new)
    # Budget gate for the (optional) cross-quarter themes LLM. On skip the §6
    # cards still render from the deterministic data — only the themes rollup is
    # forgone, surfaced via budget_skip + a note.
    themes_skip = (
        budget_gate(
            "earnings_themes_split",
            "Cross-quarter themes (§6)",
            repo_root,
            bypass=force_budget_bypass,
        )
        if enable_llm and transcripts
        else None
    )
    prepared_themes: list[ThemeRollup]
    qa_themes: list[ThemeRollup]
    themes_note: str | None
    if themes_skip is not None:
        prepared_themes, qa_themes = [], []
        themes_note = "Cross-quarter themes forgone to stay under budget — override to run."
    else:
        prepared_themes, qa_themes, themes_note = _build_themes(
            ticker=ticker,
            repo_root=repo_root,
            transcripts=transcripts,
            enable_llm=enable_llm,
            conn=conn,
        )
    return EarningsSection(
        status=SectionStatus.OK if has_any_llm else SectionStatus.PARTIAL,
        budget_skip=themes_skip,
        surprise_scorecard=surprise_card,
        full_quarters=list(reversed(full_old_to_new)),
        digest_quarters=list(reversed(digest_old_to_new)),
        prepared_remarks_themes=prepared_themes,
        qa_themes=qa_themes,
        themes_note=themes_note,
    )


def _dec_to_float(v: Decimal | None) -> float | None:
    """Decimal → float at the Pydantic boundary. None passes through."""
    return None if v is None else float(v)


def _build_surprise_card(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> SurpriseScorecardCard | None:
    """Build the §6 header beat-rate card from the `earnings_surprises` table.

    Returns None when:
      - the DB isn't reachable (open_repo_db returns None — fresh checkout)
      - there are no rows for the ticker (backfill_earnings_surprises hasn't
        run yet, or the ticker is brand new)

    Decimal-to-float conversion happens here at the compute → Pydantic
    boundary; the compute layer keeps full Decimal precision internally.
    """
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return None
    try:
        sc = surprise_scorecard_for(db_conn, ticker)
    finally:
        if conn is None:
            db_conn.close()
    if sc.total_quarters == 0:
        return None
    return SurpriseScorecardCard(
        total_quarters=sc.total_quarters,
        eps_beats=sc.eps.beats,
        eps_misses=sc.eps.misses,
        eps_no_data=sc.eps.no_data,
        eps_beat_rate_pct=_dec_to_float(sc.eps.beat_rate_pct),
        eps_avg_surprise_pct=_dec_to_float(sc.eps.avg_surprise_pct),
        eps_latest_surprise_pct=_dec_to_float(sc.eps.latest_surprise_pct),
        revenue_beats=sc.revenue.beats,
        revenue_misses=sc.revenue.misses,
        revenue_no_data=sc.revenue.no_data,
        revenue_beat_rate_pct=_dec_to_float(sc.revenue.beat_rate_pct),
        revenue_avg_surprise_pct=_dec_to_float(sc.revenue.avg_surprise_pct),
        revenue_latest_surprise_pct=_dec_to_float(sc.revenue.latest_surprise_pct),
    )


def _make_card(
    q: int,
    y: int,
    summaries: dict[tuple[int, int], str],
    transcripts: dict[tuple[int, int], str],
) -> QuarterlyEarningsCard:
    summary = summaries.get((q, y))
    return QuarterlyEarningsCard(
        quarter=f"Q{q}",
        year=y,
        summary_md=summary,
        digest_md=_extract_digest(summary) if summary else None,
        transcript_path=transcripts.get((q, y)),
    )


def _extract_digest(summary_md: str) -> str:
    """Pull just the Executive Summary block from the per-quarter summary.

    Per-quarter summaries follow a stable structure starting with
    `## 1. Executive Summary` and ending at the next H2 header. We lift that
    block verbatim — no LLM call. Falls back to the first 600 chars on miss.
    """
    lines = summary_md.splitlines()
    in_block = False
    block: list[str] = []
    for line in lines:
        if line.lstrip().startswith("## ") and "Executive Summary" in line:
            in_block = True
            continue
        if in_block and line.lstrip().startswith("## "):
            break
        if in_block:
            block.append(line)
    extracted = "\n".join(block).strip()
    if extracted:
        return extracted
    return summary_md.strip()[:600]


def _scan_summaries(tmp_dir: Path, ticker: str) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    if not tmp_dir.exists():
        return out
    upper = ticker.upper()
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        m = _SUMMARY_RX.match(path.name)
        if not m or m.group("ticker").upper() != upper:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                out[(int(m.group("q")), int(m.group("y")))] = f.read()
        except OSError:
            continue
    return out


def _scan_transcripts(tr_root: Path, ticker: str) -> dict[tuple[int, int], str]:
    """Scan both transcripts/processed/ and transcripts/raw/.

    `processed/` is the canonical promoted location (see index_manager.py); a
    file living there wins over the same (quarter, year) in `raw/`. We scan
    `raw/` second and only fill slots that processed/ left empty, matching the
    dual-dir convention `ingest_transcripts.py` already uses.
    """
    out: dict[tuple[int, int], str] = {}
    upper = ticker.upper()
    for subdir in ("processed", "raw"):
        d = tr_root / subdir
        if not d.exists():
            continue
        for path in d.iterdir():
            if not path.is_file():
                continue
            m = _TRANSCRIPT_RX.match(path.name)
            if not m or m.group("ticker").upper() != upper:
                continue
            key = (int(m.group("q")), int(m.group("y")))
            if key not in out:
                out[key] = str(path)
    return out


# ---------------------------------------------------------------------------
# Cross-quarter theme split (prepared remarks vs Q&A)
# ---------------------------------------------------------------------------


def _build_themes(
    *,
    ticker: str,
    repo_root: Path,
    transcripts: dict[tuple[int, int], str],
    enable_llm: bool,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[ThemeRollup], list[ThemeRollup], str | None]:
    """Build the prepared / Q&A theme rollups across the 4 most recent transcripts.

    Returns (prepared_themes, qa_themes, themes_note). The note is a one-line
    explanatory string the renderer surfaces above the theme blocks when a
    side is empty (e.g. "No Q&A sections available in transcripts.").

    All three are empty / None when ``enable_llm=False`` so the section can
    still render in offline / fresh-checkout mode; the LLM-pending state is
    expressed structurally (empty lists + no note) rather than as a separate
    SectionStatus so the existing OK-PARTIAL contract on EarningsSection
    doesn't need a third axis.
    """
    if not enable_llm:
        return [], [], None
    if not transcripts:
        return [], [], None

    # Take the 4 most recent (Q, Y) keys.
    keys_old_to_new = sorted(transcripts.keys(), key=lambda k: (k[1], k[0]))
    selected = keys_old_to_new[-THEMES_LOOKBACK_QUARTERS:]
    if not selected:
        return [], [], None

    qa_flags = _load_has_qa_flags(ticker, repo_root, selected, conn=conn)

    prepared_present_count = 0
    qa_present_count = 0
    payload: list[dict[str, object]] = []
    for q, y in selected:
        path = Path(transcripts[(q, y)])
        text = _read_transcript_text(path)
        if not text:
            continue
        flag = qa_flags.get((q, y))
        prepared, qa = _split_transcript_sections(text, has_qa_flag=flag)
        if prepared:
            prepared_present_count += 1
        if qa:
            qa_present_count += 1
        payload.append(
            {
                "period": f"Q{q} {y}",
                "prepared": prepared,
                "qa": qa,
            }
        )

    if not payload:
        return [], [], None

    ts_block = _ts_signals_md(ticker, repo_root, conn=conn)
    cache_path = _themes_cache_path(repo_root, ticker)
    cache_key = _themes_cache_key(payload, ts_block)
    cached = _read_themes_cache(cache_path, cache_key)
    if cached is None:
        try:
            from llm_client import extract_qa_vs_prepared_themes
        except ImportError as exc:
            log.warning({"event": "earnings_themes_split_import_failed", "error": str(exc)})
            return [], [], None
        try:
            response = extract_qa_vs_prepared_themes(ticker, payload, ts_signals_md=ts_block)
        except Exception as exc:  # surface but don't break the rest of §5
            # Hard stops (monthly budget cap, CLI not installed) must propagate
            # — re-running won't help and degrading would mask an over-budget run.
            if is_hard_stop(exc):
                raise
            log.error(
                {"event": "earnings_themes_split_failed", "ticker": ticker, "error": str(exc)}
            )
            return [], [], None
        # Parse at SECTION scope, separate from the call's exception handler
        # above. `_parse_themes_response` is internally total today (it returns
        # empty rollups on an empty / non-JSON / wrong-shape payload), but this
        # guard makes the safety structural: a future change that lets the
        # parser raise must still degrade to empty theme rollups — the §6 cards
        # render regardless — and never abort the whole multi-section build.
        # Mirrors the §7 bear-case fix (PR #197). Hard call-exceptions are
        # handled by the `except Exception` above, not here.
        try:
            parsed = _parse_themes_response(response)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(
                {"event": "earnings_themes_parse_degraded", "ticker": ticker, "error": str(exc)}
            )
            empty_rollups: tuple[list[ThemeRollup], list[ThemeRollup]] = ([], [])
            parsed = empty_rollups
        _write_themes_cache(cache_path, cache_key, parsed)
    else:
        parsed = cached

    prepared_themes, qa_themes = parsed
    # Defensive: if a side had no input across the whole window, force its
    # rollup to empty regardless of what the LLM returned (it should have
    # respected the instruction but we don't trust the prompt to be
    # invariant under malformed inputs).
    if prepared_present_count == 0:
        prepared_themes = []
    if qa_present_count == 0:
        qa_themes = []

    note: str | None = None
    if qa_present_count == 0 and prepared_present_count > 0:
        note = "No Q&A sections available in transcripts."
    elif prepared_present_count == 0 and qa_present_count > 0:
        note = "No prepared-remarks sections available in transcripts."
    elif prepared_present_count == 0 and qa_present_count == 0:
        note = (
            "Transcripts available but no recognizable prepared-remarks or Q&A sections detected."
        )

    return prepared_themes, qa_themes, note


def _load_has_qa_flags(
    ticker: str,
    repo_root: Path,
    periods: list[tuple[int, int]],
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[tuple[int, int], bool | None]:
    """Look up ``transcripts.has_qa_section`` for each requested (quarter, year).

    Returns a dict mapping (q, y) → bool|None (the tri-state from migration
    0019). Missing rows / unreadable DB → empty dict; the caller treats
    unknown as "no DB-side signal, fall back to in-file detection".
    """
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return {}
    out: dict[tuple[int, int], bool | None] = {}
    try:
        if not _has_transcripts_table(db_conn):
            return {}
        transcripts = selected_transcripts_relation(db_conn).sql
        for q, y in periods:
            try:
                row = db_conn.execute(
                    f"SELECT has_qa_section, period_end FROM {transcripts} "  # nosec B608 -- trusted internal SQL shape; values remain bound
                    "WHERE ticker = ? AND fiscal_period_type = ? "
                    "ORDER BY period_end DESC LIMIT 5",
                    (ticker.upper(), f"Q{q}"),
                ).fetchall()
            except sqlite3.Error as exc:
                log.debug(
                    {"event": "themes_qa_flag_lookup_failed", "ticker": ticker, "error": str(exc)}
                )
                continue
            # Pick the row whose period_end falls in the requested fiscal year.
            # Calendar-year fallback: the filename `_QN_YYYY` carries the
            # fiscal-year label; we accept any row matching the (Q, Y) by
            # picking the most recent matching period_end. For exotic FYE
            # tickers (VEEV, RBRK, TOL) the period_end won't match the
            # label year exactly — leave the flag as None and the splitter
            # will fall back to in-file markers.
            for r in row:
                period_end = r["period_end"]
                pe_str = str(period_end)[:4]
                if pe_str == str(y):
                    flag = r["has_qa_section"]
                    out[(q, y)] = None if flag is None else bool(flag)
                    break
    finally:
        if conn is None:
            db_conn.close()
    return out


def _has_transcripts_table(conn: sqlite3.Connection) -> bool:
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transcripts'"
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _read_transcript_text(path: Path) -> str | None:
    """Best-effort read; PDFs go through parser.extract_text_from_pdf (matches
    the appendix's read path). Failures degrade silently — themes is a
    nice-to-have, the rest of §5 still renders."""
    if not path.exists():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            extract = cast(
                "Callable[[str], str]",
                importlib.import_module("parser").extract_text_from_pdf,
            )
            return extract(str(path))
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        log.debug({"event": "themes_transcript_read_failed", "path": str(path), "error": str(exc)})
        return None


def _split_transcript_sections(
    text: str, *, has_qa_flag: bool | None
) -> tuple[str | None, str | None]:
    """Split a transcript into (prepared_remarks, qa) text.

    Detection ladder:
      1. Aggregator Q&A-only banner — entire file is Q&A; prepared=None.
      2. CallStreet `QUESTION AND ANSWER SECTION` header — split at it.
      3. Plain `=== Q&A SEGMENT ===` marker without the Q&A-only banner —
         split at it (in case a future aggregator format includes prepared).
      4. Fall back to ``has_qa_flag``: True → entire file is Q&A;
         False → entire file is prepared; None → conservative default of
         prepared (no Q&A signal anywhere).

    Returns (None, None) for empty input. Either side may be None when
    that section is absent in the source.
    """
    if not text.strip():
        return None, None

    if _AGGREGATOR_QA_ONLY_BANNER.search(text):
        marker = _AGGREGATOR_QA_SEGMENT_MARKER.search(text)
        if marker is not None:
            qa_body = text[marker.end() :].strip()
        else:
            # Banner present but no explicit marker — treat everything after
            # the banner line as Q&A.
            banner = _AGGREGATOR_QA_ONLY_BANNER.search(text)
            assert banner is not None
            after_banner = text[banner.end() :]
            nl = after_banner.find("\n")
            qa_body = (after_banner[nl + 1 :] if nl >= 0 else after_banner).strip()
        return None, qa_body or None

    cs_match = _CALLSTREET_QA_HEADER.search(text)
    if cs_match is not None:
        prepared = text[: cs_match.start()].strip()
        qa = text[cs_match.end() :].strip()
        return (prepared or None), (qa or None)

    marker = _AGGREGATOR_QA_SEGMENT_MARKER.search(text)
    if marker is not None:
        prepared = text[: marker.start()].strip()
        qa = text[marker.end() :].strip()
        return (prepared or None), (qa or None)

    # Free-form full transcripts (no explicit section header) sometimes
    # transition via "first question comes from X" — split at the first
    # such occurrence so prepared remarks aren't lost into the Q&A bucket.
    first_q = _OPERATOR_FIRST_QUESTION.search(text)
    if first_q is not None:
        prepared = text[: first_q.start()].strip()
        qa = text[first_q.start() :].strip()
        return (prepared or None), (qa or None)

    if has_qa_flag is True:
        return None, text.strip()
    # has_qa_flag in (False, None) — treat entire file as prepared remarks
    # because we have no positive signal of Q&A content.
    return text.strip(), None


def _themes_cache_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "data" / "earnings_themes" / f"{ticker.upper()}.json"


def _earnings_metric_names(ticker: str, repo_root: Path) -> list[str]:
    """Compose the metric-name list the earnings TS block should load.

    Headline P&L + cash + the per-ticker tier_1 KPIs from the holdings
    JSON. Tier-1 KPIs anchor the quarter against thesis-critical series;
    revenue / OI / EPS / FCF / margins are the universal headline cuts
    every print is read against.
    """
    names: list[str] = [
        "revenue",
        "operating_income",
        "free_cash_flow",
        "net_income",
        "gross_profit",
    ]
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not holdings_path.exists():
        return names
    try:
        holdings = cast("dict[str, object]", json.loads(holdings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return names
    raw_kpis = holdings.get("tier_1_kpis")
    if not isinstance(raw_kpis, list):
        return names
    seen: set[str] = set(names)
    for k in cast("list[object]", raw_kpis):
        if not isinstance(k, dict):
            continue
        name = cast("dict[str, object]", k).get("name")
        if isinstance(name, str) and name.strip() and name not in seen:
            names.append(name.strip())
            seen.add(name)
    return names


def _ts_signals_md(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Render the headline-P&L + tier-1 KPI signals as a markdown block.

    Surfaced to the prepared-vs-Q&A themes prompt so the LLM can
    interpret what management chose to lead with (and what analysts
    pressed on) against the trailing trend / inflection / anomaly state.
    Empty string when no signals exist for any requested metric.
    """
    grouped = load_signals_for_metrics(
        ticker,
        _earnings_metric_names(ticker, repo_root),
        repo_root=repo_root,
        conn=conn,
    )
    flat = [s for metric in grouped.values() for s in metric]
    return format_signals_as_prompt_block(
        flat, heading="Time-Series Context for Quarterly Interpretation"
    )


def _themes_cache_key(payload: list[dict[str, object]], ts_signals_md: str = "") -> str:
    """Hash of the LLM input — invalidates cache on any source change.

    Hashes the complete prepared and Q&A inputs. Transcript re-extraction can
    change analysis-relevant text anywhere in the body, so prefix sampling is
    not a safe cache identity.

    ``ts_signals_md`` is folded in as the full block text so a refresh of
    the timeseries_signals table (different narratives / severities)
    forces a re-extraction even when transcripts are unchanged.
    """
    h = hashlib.sha256()
    for entry in payload:
        period = str(entry.get("period") or "")
        prepared = entry.get("prepared")
        qa = entry.get("qa")
        h.update(period.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(len(prepared) if isinstance(prepared, str) else 0).encode("utf-8"))
        h.update(b"\x00")
        h.update(str(len(qa) if isinstance(qa, str) else 0).encode("utf-8"))
        h.update(b"\x00")
        if isinstance(prepared, str):
            h.update(prepared.encode("utf-8", errors="ignore"))
        h.update(b"\x00")
        if isinstance(qa, str):
            h.update(qa.encode("utf-8", errors="ignore"))
        h.update(b"\xff")
    h.update(b"|TS|")
    h.update(ts_signals_md.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _read_themes_cache(
    path: Path, expected_key: str
) -> tuple[list[ThemeRollup], list[ThemeRollup]] | None:
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None
    if render_now() - mtime > timedelta(days=THEMES_CACHE_TTL_DAYS):
        return None
    try:
        raw_body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_body, dict):
        return None
    body = cast("dict[str, object]", raw_body)
    if body.get("cache_key") != expected_key:
        return None
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return None
    return _parse_themes_payload(cast("dict[str, object]", payload))


def _write_themes_cache(
    path: Path, cache_key: str, parsed: tuple[list[ThemeRollup], list[ThemeRollup]]
) -> None:
    prepared, qa = parsed
    body = {
        "cache_key": cache_key,
        "payload": {
            "prepared_themes": [_theme_to_json(t) for t in prepared],
            "qa_themes": [_theme_to_json(t) for t in qa],
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.debug({"event": "themes_cache_write_failed", "path": str(path), "error": str(exc)})


def _theme_to_json(t: ThemeRollup) -> dict[str, object]:
    return {
        "theme_name": t.theme_name,
        "last_4q_count": t.last_4q_count,
        "mentions_per_quarter": t.mentions_per_quarter,
        "evidence": [
            {"period": q.period, "speaker": q.speaker, "text": q.text} for q in t.evidence
        ],
    }


def _parse_themes_response(raw: str) -> tuple[list[ThemeRollup], list[ThemeRollup]]:
    """Parse the LLM JSON envelope into (prepared, qa) rollups.

    Tolerates a leading / trailing markdown fence (the prompt asks for raw
    JSON but Claude occasionally wraps despite the instruction). Returns
    ([], []) when the payload is malformed — the section still renders
    cards in that case, just without theme blocks.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning({"event": "earnings_themes_parse_failed", "error": str(exc)})
        return [], []
    if not isinstance(payload, dict):
        return [], []
    return _parse_themes_payload(cast("dict[str, object]", payload))


def _parse_themes_payload(
    payload: dict[str, object],
) -> tuple[list[ThemeRollup], list[ThemeRollup]]:
    prepared = _coerce_theme_list(payload.get("prepared_themes"))
    qa = _coerce_theme_list(payload.get("qa_themes"))
    return prepared, qa


def _coerce_theme_list(raw: object) -> list[ThemeRollup]:
    if not isinstance(raw, list):
        return []
    out: list[ThemeRollup] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        name = e.get("theme_name")
        if not isinstance(name, str) or not name.strip():
            continue
        mentions_raw = e.get("mentions_per_quarter")
        mentions: dict[str, int] = {}
        if isinstance(mentions_raw, dict):
            for k, v in cast("dict[object, object]", mentions_raw).items():
                if not isinstance(k, str):
                    continue
                try:
                    mentions[k] = int(cast("int | str", v))
                except (TypeError, ValueError):
                    continue
        # last_4q_count comes from the LLM when present; otherwise derive
        # from mentions sum so the renderer's headline chip is always set.
        cnt_raw = e.get("last_4q_count")
        if isinstance(cnt_raw, int):
            cnt = cnt_raw
        else:
            try:
                cnt = (
                    int(cast("int | str", cnt_raw))
                    if cnt_raw is not None
                    else sum(mentions.values())
                )
            except (TypeError, ValueError):
                cnt = sum(mentions.values())
        evidence_raw = e.get("evidence")
        evidence: list[QuoteSnippet] = []
        if isinstance(evidence_raw, list):
            for q_entry in cast("list[object]", evidence_raw):
                if not isinstance(q_entry, dict):
                    continue
                qe = cast("dict[str, object]", q_entry)
                period = qe.get("period")
                text_val = qe.get("text")
                if not isinstance(period, str) or not isinstance(text_val, str):
                    continue
                speaker = qe.get("speaker")
                evidence.append(
                    QuoteSnippet(
                        period=period.strip(),
                        text=text_val.strip(),
                        speaker=speaker.strip()
                        if isinstance(speaker, str) and speaker.strip()
                        else None,
                    )
                )
        out.append(
            ThemeRollup(
                theme_name=name.strip(),
                last_4q_count=cnt,
                mentions_per_quarter=mentions,
                evidence=evidence,
            )
        )
    return out

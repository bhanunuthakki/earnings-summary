"""
src/llm/anchors.py
------------------
Anchor block builders — shared context for analytical prompts. Four flavors:

  - THESIS anchor:  analyst's own framing from `micro_thesis/holdings/<T>.json`
                    (thesis statement, tier-1 KPIs, business-model break rules,
                    statistical patterns over the canonical financial series).
  - BEAR anchor:    analyst's prior bear review from `data/bear_case/<T>.json`
                    (most-underweighted thesis risks + named failure modes).
  - IR anchor:      company-provided IR-deck narrative from
                    `data/ir_narrative/<T>/` — what management says about
                    itself. Prefixed with a bias-framing header so the LLM
                    treats it as company spin rather than ground truth.
  - PRIORS anchor:  the analyst's OPEN notes from the durable analyst_notes
                    table (alembic 0074) — open questions, watch-items,
                    assumptions, standing decisions. Framed as "engage,
                    don't re-litigate": answer a question when the data
                    permits, flag confirmation/contradiction of watch-items.

The anchors compose into a single block via `compose_anchor_block`. The
prompts that promise "thesis-anchored analysis" (per-quarter summary, pairwise
SayDo, recent developments, SayDo filter, event brief, company description,
bear case) now ground in them.

All loaders are tolerant: missing files / DB / table → empty string, so
watchlist / evaluation / recently-IPO'd tickers still build without anchor
sections.

Public API:
    load_thesis_anchor(repo_root, ticker) -> str
    load_bear_anchor(repo_root, ticker) -> str
    load_ir_anchor(repo_root, ticker, char_cap=IR_ANCHOR_CHAR_CAP) -> str
    load_priors_anchor(repo_root, ticker, char_cap=PRIORS_ANCHOR_CHAR_CAP) -> str
    compose_anchor_block(thesis_anchor, bear_anchor, ir_anchor="",
                         priors_anchor="") -> str
    ANCHOR_BLOCK_CHAR_CAP    — hard cap on thesis / bear blocks (3500).
    IR_ANCHOR_CHAR_CAP        — hard cap on IR block (2000, deliberately
                                downweighted vs analyst-authored anchors).
    PRIORS_ANCHOR_CHAR_CAP    — hard cap on the priors block (2000).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)

# Hard cap on the assembled anchor blocks so a verbose holdings JSON cannot
# blow the prompt budget on any single prompt. Trim is deterministic
# (truncation at the section boundary), not a smart compressor.
ANCHOR_BLOCK_CHAR_CAP = 3500

# IR-narrative anchor is deliberately tighter (~2K vs 3.5K). The content is
# company-biased framing, so we want it present in the context but downweighted
# relative to the analyst's own thesis + bear blocks.
IR_ANCHOR_CHAR_CAP = 2000

# Priors anchor (open analyst notes) — compact by design: the anchor stack
# already carries thesis + bear + IR on most prompts, and the highest-value
# priors are short (a question, a watch-item). Newest notes win under the cap.
PRIORS_ANCHOR_CHAR_CAP = 2000

_HOLDINGS_DIRNAME = ("micro_thesis", "holdings")
_BEAR_CASE_CACHE_DIRNAME = ("data", "bear_case")
_IR_NARRATIVE_DIRNAME = ("data", "ir_narrative")

# Loader walks these doctypes in priority order — the first one with cached
# narrative for the ticker wins. Maps the user-facing taxonomy
# (deck > investor day > fact sheet > ESG) onto the on-disk doctype names
# emitted by `src/intake.py` and cached by `src/compute/ir_narrative.py`.
_IR_DOCTYPE_PRIORITY: tuple[str, ...] = (
    "ir_presentation",
    "ir_event",
    "ir_investor_update",
)

# Fixed disambiguation header — prepended to every IR-anchor return so each
# consumer sees the same skepticism frame. Keep this prose tight; every char
# counts against IR_ANCHOR_CHAR_CAP.
_IR_BIAS_HEADER = """## IR ANCHOR (company-provided framing — USE WITH SKEPTICISM)

The following is taken from investor-relations materials authored by management.
It reflects how the company chooses to present itself — TAM claims tend to be
top-of-market sized, strategic priorities are aspirational, competitive
positioning is self-favorable, and risk language is softened. Use this to
understand *what the company says* and *how they frame it*, not as ground
truth. Form your own POV; cross-check claims against the 10-K, third-party
data, and historical execution."""


def _load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read ``micro_thesis/holdings/<TICKER>.json`` defensively. Returns None
    for any read or parse failure so callers degrade to no-anchor mode."""
    path = repo_root.joinpath(*_HOLDINGS_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _kpi_anchor_lines(payload: dict[str, object]) -> list[str]:
    """Render `tier_1_kpis` as one-line bullets: name + break condition."""
    raw = payload.get("tier_1_kpis")
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        name = e.get("name")
        bc = e.get("break_condition")
        if not isinstance(name, str) or not name.strip():
            continue
        bc_text = bc.strip() if isinstance(bc, str) and bc.strip() else "—"
        lines.append(f"- **{name.strip()}** — breaks if {bc_text}")
    return lines


def _business_rule_anchor_lines(payload: dict[str, object]) -> list[str]:
    """Render quantitative `business_model_rules` as scannable bullets."""
    raw = payload.get("business_model_rules")
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        narrative = e.get("narrative")
        if isinstance(narrative, str) and narrative.strip():
            lines.append(f"- {narrative.strip()}")
    return lines


# Canonical financial line items always worth statistically profiling — the
# Week-1 time-series layer runs detect_trend + detect_inflection on these
# so the LLM sees the numerical read, not just the raw rows. Stays a small
# fixed set so the anchor block doesn't balloon; per-ticker tier-1 KPIs
# are added on top via load_kpi_series when available.
_STATS_LINE_ITEMS: tuple[str, ...] = (
    "revenue",
    "operating_income",
    "free_cash_flow",
    "net_income",
)


def _stats_block_from_series(series_label: str, series: list[object]) -> str | None:
    """One-line markdown summary of a series: direction + slope + (inflection
    when present). Returns None when the series is too short to analyze."""
    from timeseries import detect_inflection, detect_trend

    if len(series) < 4:
        return None
    trend = detect_trend(cast("list[object]", series))
    if trend.get("insufficient_data"):
        return None
    direction = str(trend.get("direction") or "?")
    slope_pct = trend.get("slope_pct_of_mean")
    slope_str = (
        f"{float(cast('float', slope_pct)) * 100:+.1f}%/q"
        if isinstance(slope_pct, (int, float))
        else "—"
    )
    sig = " (sig)" if trend.get("statistical_significance") else ""
    line = f"- **{series_label}** — {direction} · slope {slope_str}{sig}"

    # Add inflection callout when the series is long enough
    if len(series) >= 8:
        infl = detect_inflection(cast("list[object]", series))
        if (
            infl.get("inflection_period")
            and float(cast("float", infl.get("magnitude") or 0)) >= 1.0
        ):
            line += f" · inflection {infl['inflection_period']} (delta={float(cast('float', infl['magnitude'])):.1f}sd)"
    return line


def _statistical_patterns_block(
    repo_root: Path, ticker: str, payload: dict[str, object]
) -> list[str]:
    """Run detect_trend + detect_inflection on the canonical line items and
    any per-ticker registered KPIs. Returns markdown lines, empty when no
    series load (missing DB, unknown ticker)."""
    try:
        from timeseries import load_financial_series, load_kpi_series
    except ImportError:
        return []

    lines: list[str] = []
    for line_item in _STATS_LINE_ITEMS:
        try:
            s = load_financial_series(ticker=ticker, line_item=line_item, repo_root=repo_root)
        except Exception as exc:  # never block anchor build
            log.debug(
                {
                    "event": "stats_load_financial_failed",
                    "ticker": ticker,
                    "lineitem": line_item,
                    "error": str(exc),
                }
            )
            continue
        if not s:
            continue
        rendered = _stats_block_from_series(line_item.replace("_", " "), cast("list[object]", s))
        if rendered:
            lines.append(rendered)

    # Per-ticker registered KPIs (from kpi_definitions). Tier-1 KPIs from the
    # holdings JSON would be ideal here but holdings names rarely match the
    # registry verbatim — use the registry as the source of truth.
    db_path = repo_root / "data" / "portfolio.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT name FROM kpi_definitions WHERE ticker = ? LIMIT 4",
                    (ticker.upper(),),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.debug({"event": "stats_kpi_def_lookup_failed", "error": str(exc)})
            rows = []
        for (kpi_name,) in rows:
            if not isinstance(kpi_name, str) or not kpi_name.strip():
                continue
            try:
                s_kpi = load_kpi_series(ticker=ticker, kpi_name=kpi_name, repo_root=repo_root)
            except Exception as exc:  # best-effort
                log.debug(
                    {
                        "event": "stats_load_kpi_failed",
                        "ticker": ticker,
                        "kpi": kpi_name,
                        "error": str(exc),
                    }
                )
                continue
            if not s_kpi:
                continue
            short_label = kpi_name if len(kpi_name) <= 60 else kpi_name[:57] + "…"
            rendered = _stats_block_from_series(short_label, cast("list[object]", s_kpi))
            if rendered:
                lines.append(rendered)

    # Reference payload so linters know it's intentional (reserved for
    # future use: cross-check tier-1 KPI names against the registry)
    _ = payload
    return lines


def load_thesis_anchor(repo_root: Path, ticker: str) -> str:
    """Compose a compact thesis anchor for prompt injection. Empty string
    when no holdings JSON exists. Output is markdown, ~300-1500 chars.

    Since the Week-1 time-series layer landed, the anchor also carries a
    "Recent statistical patterns" subsection — detect_trend + detect_inflection
    over the canonical financial line items + any per-ticker registered KPIs.
    The subsection is best-effort: DB / loader failures degrade silently so
    the original thesis anchor still renders."""
    payload = _load_holdings_json(repo_root, ticker)
    if payload is None:
        return ""

    parts: list[str] = ["## THESIS ANCHOR (analyst's own framing of this name)"]

    # Recently-IPO'd issuers: tell the LLM the narrative source is the S-1,
    # not the 10-K — so it stops phrasing claims as "the company's most-recent
    # 10-K disclosed..." which would be factually wrong.
    data_anchor = payload.get("data_anchor")
    if isinstance(data_anchor, str) and data_anchor.strip().lower() == "s1":
        ipo_date = payload.get("ipo_date")
        ipo_suffix = (
            f" (IPO {ipo_date.strip()})"
            if isinstance(ipo_date, str) and ipo_date.strip()
            else ""
        )
        parts.append(
            f"\n**Narrative source:** S-1 / 424B prospectus{ipo_suffix} — "
            f"no 10-K filed yet. Phrase historical claims accordingly."
        )

    thesis = payload.get("thesis")
    if isinstance(thesis, str) and thesis.strip():
        parts.append(f"\n**Thesis statement:**\n{thesis.strip()}")

    key_driver = payload.get("key_driver")
    if isinstance(key_driver, str) and key_driver.strip():
        parts.append(f"\n**Key driver tracked:** {key_driver.strip()}")

    kpi_lines = _kpi_anchor_lines(payload)
    if kpi_lines:
        parts.append("\n**Tier-1 KPIs (with break conditions):**")
        parts.extend(kpi_lines)

    rule_lines = _business_rule_anchor_lines(payload)
    if rule_lines:
        parts.append("\n**Quantitative thesis-breakers:**")
        parts.extend(rule_lines)

    # Best-effort statistical block. Wrapped in a try/except so any failure
    # in the timeseries layer (DB missing, scipy import error, etc.) can't
    # break the anchor for the dozens of prompts that depend on it.
    try:
        stats_lines = _statistical_patterns_block(repo_root, ticker, payload)
    except Exception as exc:  # anchor must keep rendering
        log.debug(
            {"event": "statistical_patterns_block_failed", "ticker": ticker, "error": str(exc)}
        )
        stats_lines = []
    if stats_lines:
        parts.append("\n**Recent statistical patterns (last 8-16 quarters):**")
        parts.extend(stats_lines)

    if len(parts) == 1:  # only the header — no usable content
        return ""

    assembled = "\n".join(parts).strip()
    if len(assembled) > ANCHOR_BLOCK_CHAR_CAP:
        assembled = assembled[:ANCHOR_BLOCK_CHAR_CAP].rstrip() + "\n[...truncated]"
    return assembled


def load_bear_anchor(repo_root: Path, ticker: str) -> str:
    """Compose a compact bear-case anchor from the on-disk cache (written by
    the bear_case section after a successful LLM run). Returns the
    `most_underweighted` paragraph plus the top 3 failure-mode hypotheses so
    the per-quarter summary / news / SayDo can engage with the analyst's
    existing bear framing without re-running the bear case.

    Returns "" when no cache exists (no prior `--enable-llm` run) so the
    first-ever build of a ticker still works without circular dependency.
    """
    path = repo_root.joinpath(*_BEAR_CASE_CACHE_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return ""
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ""

    parts: list[str] = ["## BEAR-CASE ANCHOR (from analyst's prior bear review)"]

    underweighted = payload.get("most_underweighted")
    if isinstance(underweighted, str) and underweighted.strip():
        parts.append(f"\n**Most underweighted by consensus:**\n{underweighted.strip()}")

    fms = payload.get("failure_modes")
    if isinstance(fms, list):
        hyps: list[str] = []
        for entry in cast("list[object]", fms)[:3]:
            if not isinstance(entry, dict):
                continue
            e = cast("dict[str, object]", entry)
            h = e.get("hypothesis")
            if isinstance(h, str) and h.strip():
                hyps.append(f"- {h.strip()}")
        if hyps:
            parts.append("\n**Named failure modes the analyst is tracking:**")
            parts.extend(hyps)

    if len(parts) == 1:
        return ""

    assembled = "\n".join(parts).strip()
    if len(assembled) > ANCHOR_BLOCK_CHAR_CAP:
        assembled = assembled[:ANCHOR_BLOCK_CHAR_CAP].rstrip() + "\n[...truncated]"
    return assembled


def load_ir_anchor(repo_root: Path, ticker: str, char_cap: int = IR_ANCHOR_CHAR_CAP) -> str:
    """Compose an IR-narrative anchor for prompt injection.

    Reads from `data/ir_narrative/<TICKER>/` (written by
    `src/compute/ir_narrative.py`). Picks the most-recent narrative for the
    ticker, walking doctypes in priority order:
    ``ir_presentation > ir_event > ir_investor_update``. Within a doctype, the
    lexicographically-largest filename wins — period strings are ISO dates
    (YYYY-MM-DD), so lex order is also chronological order.

    The output has the bias-framing header prepended so every consumer treats
    the content as company spin rather than ground truth.

    Returns "" when no cache exists for the ticker — recently-IPO'd names
    typically rely on the S-1 anchor instead (see project-recently-ipod-anchor
    memory). The S-1 path is intentionally separate so the IR loader stays
    focused on a single source.
    """
    base = repo_root.joinpath(*_IR_NARRATIVE_DIRNAME) / ticker.upper()
    if not base.is_dir():
        return ""

    by_doctype: dict[str, list[Path]] = {dt: [] for dt in _IR_DOCTYPE_PRIORITY}
    try:
        entries = list(base.iterdir())
    except OSError:
        return ""
    for path in entries:
        if not path.is_file() or path.suffix != ".txt":
            continue
        for doctype in _IR_DOCTYPE_PRIORITY:
            if path.name.startswith(f"{doctype}__"):
                by_doctype[doctype].append(path)
                break

    selected: Path | None = None
    for doctype in _IR_DOCTYPE_PRIORITY:
        candidates = by_doctype.get(doctype, [])
        if not candidates:
            continue
        selected = sorted(candidates)[-1]
        break

    if selected is None:
        return ""

    try:
        body = selected.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not body:
        return ""

    # Reserve room for the header + a one-line source tag so the LLM can cite
    # which deck the framing came from. The minimum body budget (~200 chars)
    # keeps the section meaningful even if the caller passes a tight cap.
    tag_line = f"\n_Source: {selected.stem}_\n\n"
    header_overhead = len(_IR_BIAS_HEADER) + len(tag_line) + 4  # newlines
    body_budget = max(char_cap - header_overhead, 200)
    if len(body) > body_budget:
        body = body[:body_budget].rstrip() + "\n[...truncated]"

    return f"{_IR_BIAS_HEADER}\n{tag_line}{body}"


# Fixed framing header — the priors block tells the model how to USE the
# analyst's open notes, not just what they are. Kept tight; every char counts
# against PRIORS_ANCHOR_CHAR_CAP.
_PRIORS_HEADER = """## ANALYST PRIORS (the analyst's own open notes — engage, don't re-litigate)

These are questions, watch-items, assumptions, and standing decisions the
analyst has already recorded. Treat them as the analyst's current state of
mind: answer an open question when the data at hand permits (and say which
question you are answering), call out explicitly when evidence confirms or
contradicts a watch-item or assumption, and do not re-explain or argue
against a standing decision."""

# Render order: actionable items first. Labels are the prompt-facing names.
_PRIORS_KIND_ORDER: tuple[tuple[str, str], ...] = (
    ("question", "Open questions"),
    ("watch", "Watch-items"),
    ("assumption", "Assumptions"),
    ("decision", "Standing decisions"),
    ("observation", "Observations"),
)


def load_priors_anchor(repo_root: Path, ticker: str, char_cap: int = PRIORS_ANCHOR_CHAR_CAP) -> str:
    """Compose the analyst-priors anchor from OPEN analyst_notes rows.

    Reads the durable notes table (alembic 0074; populated by the comment
    reconciler and manual capture). Open notes only — resolved/archived
    history belongs to the report surfaces, not to every prompt.

    Returns "" when the DB or table is absent, the substrate predates 0074,
    or there are simply no open notes — and never raises: like the other
    loaders, dozens of prompts depend on anchor assembly staying unkillable.

    Cache-stability invariant: several LLM caches key on the composed anchor
    text, so every rendered element must be stable until the notes actually
    change — lines carry the note's creation DATE, never a relative age.
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return ""
    try:
        from user_state.notes import list_notes

        rows = list_notes(ticker=ticker, status="open", limit=40, db_path=db_path)
    except Exception as exc:  # missing table / locked DB / anything — degrade
        log.debug({"event": "priors_anchor_load_failed", "ticker": ticker, "error": str(exc)})
        return ""
    if not rows:
        return ""

    by_kind: dict[str, list[str]] = {}
    for r in rows:
        body = " ".join(r.body.split())
        if len(body) > 240:
            body = body[:237].rstrip() + "..."
        where = (
            f" [{r.anchor_key}]"
            if r.anchor_key and r.anchor_type not in (None, "free_text")
            else ""
        )
        by_kind.setdefault(r.kind, []).append(
            f"- {body}{where} (since {r.created_at.date().isoformat()})"
        )

    parts: list[str] = [_PRIORS_HEADER]
    for kind, label in _PRIORS_KIND_ORDER:
        lines = by_kind.get(kind)
        if lines:
            parts.append(f"\n**{label}:**")
            parts.extend(lines)
    if len(parts) == 1:
        return ""

    assembled = "\n".join(parts).strip()
    if len(assembled) > char_cap:
        assembled = assembled[:char_cap].rstrip() + "\n[...truncated]"
    return assembled


def compose_anchor_block(
    thesis_anchor: str, bear_anchor: str, ir_anchor: str = "", priors_anchor: str = ""
) -> str:
    """Join thesis + bear + IR + priors anchors with separators, omitting
    empties. Returns "" when all are empty so the caller can conditionally
    insert.

    `ir_anchor` and `priors_anchor` are keyword-defaulted positional args so
    legacy 2- and 3-arg call sites that pre-date those layers keep working
    unchanged. New callers should pass all four; the conventional builder
    pattern is

        compose_anchor_block(
            load_thesis_anchor(repo_root, ticker),
            load_bear_anchor(repo_root, ticker),
            load_ir_anchor(repo_root, ticker),
            load_priors_anchor(repo_root, ticker),
        )

    The composed block is spotlighted (``llm.untrusted.spotlight``) before it
    ships: anchors chain LLM artifacts and issuer-authored IR narrative into
    ~7 downstream prompts, so an injection surviving into any anchor source
    would otherwise propagate with instruction authority. The wrap is
    deterministic — artifact caches that key on the composed anchor text stay
    stable for unchanged anchors.
    """
    from llm.untrusted import spotlight

    blocks = [b for b in (thesis_anchor, bear_anchor, ir_anchor, priors_anchor) if b.strip()]
    if not blocks:
        return ""
    wrapped = spotlight(
        "\n\n---\n\n".join(blocks),
        source=(
            "stored research context (analyst thesis, prior bear-case review, "
            "IR narrative, analyst notes)"
        ),
    )
    return wrapped + "\n\n---\n\n"

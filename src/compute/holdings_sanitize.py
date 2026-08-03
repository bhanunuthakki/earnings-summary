"""Persist-time scalar hygiene for ``micro_thesis/holdings/<TICKER>.json``.

LLM edit routes (``execution/process_report_comments.py``) author strings in
the holdings JSON, and markdown was observed leaking into SCALAR fields — a
chart priority stored as ``**Priority #1 — Mexico momentum**`` renders raw in
every surface that treats the field as plain text. The rule across persist
paths: scalars are plain, prose fields keep markdown.

Field classification for the holdings payload:

SCALAR (stripped here):
    chart_priorities[]              KPI display names (order matters)
    competitive_watchlist[]         rival names / tickers
    peer_exclude[]                  rival names / tickers
    thesis_breakers_qualitative[]   string entries only ("free shape" — dict
                                    entries are left alone)
    tier_1/2/3_kpis[].name          KPI display names (become
                                    kpi_definitions.name via seed_kpi_definitions)
    break_rules[].kpi_name          joined against kpi_facts by name
    break_rules_soft[].kpi_name

PROSE / never touched here:
    thesis                          narrative body (render_prose boundary)
    tier_N_kpis[].break_condition   short analytical phrases
    break_rules[].narrative         one analytical sentence
    everything else (verdict, wacc, dcf_defaults, ... — code-authored)
"""

from __future__ import annotations

from typing import cast

from llm.postprocess import strip_inline_markdown

__all__ = ["sanitize_holdings_scalars"]

_STR_LIST_FIELDS: tuple[str, ...] = (
    "chart_priorities",
    "competitive_watchlist",
    "peer_exclude",
    "thesis_breakers_qualitative",
)
_KPI_TIER_FIELDS: tuple[str, ...] = ("tier_1_kpis", "tier_2_kpis", "tier_3_kpis")
_RULE_FIELDS: tuple[str, ...] = ("break_rules", "break_rules_soft")


def _strip_key(entry: object, key: str, path: str, changed: list[str]) -> None:
    if not isinstance(entry, dict):
        return
    typed = cast("dict[str, object]", entry)
    value = typed.get(key)
    if isinstance(value, str):
        plain = strip_inline_markdown(value)
        if plain != value:
            typed[key] = plain
            changed.append(path)


def sanitize_holdings_scalars(payload: dict[str, object]) -> list[str]:
    """Strip inline markdown from the scalar fields listed above, in place.

    Returns the JSON paths that changed (empty list = payload untouched), so
    callers can log what was rewritten rather than sanitizing silently.
    """
    changed: list[str] = []
    for field in _STR_LIST_FIELDS:
        raw = payload.get(field)
        if not isinstance(raw, list):
            continue
        items = cast("list[object]", raw)
        for i, item in enumerate(items):
            if isinstance(item, str):
                plain = strip_inline_markdown(item)
                if plain != item:
                    items[i] = plain
                    changed.append(f"{field}[{i}]")
    for field in _KPI_TIER_FIELDS:
        raw = payload.get(field)
        if isinstance(raw, list):
            for i, entry in enumerate(cast("list[object]", raw)):
                _strip_key(entry, "name", f"{field}[{i}].name", changed)
    for field in _RULE_FIELDS:
        raw = payload.get(field)
        if isinstance(raw, list):
            for i, entry in enumerate(cast("list[object]", raw)):
                _strip_key(entry, "kpi_name", f"{field}[{i}].kpi_name", changed)
    return changed

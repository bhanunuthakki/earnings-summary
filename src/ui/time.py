"""Human time rendering — the platform-wide convention (UX redesign PR1).

Three rules, applied everywhere a timestamp reaches the DOM:
  1. ACTIVITY ("when did this fire/run/build") renders RELATIVE — "2h ago".
  2. FACTS ("which date is this about") render as CALENDAR dates — "Jun 8, 2026".
  3. The exact UTC instant lives in the hover tooltip (title attr), never inline.

Raw second-precision ISO stamps ("2026-06-11T05:25:45+00:00") in HTML are a
rendering bug by convention. JSON/API payloads keep full ISO — this module is
for the DOM only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from report.renderers.numfmt import fmt_date, fmt_reltime

__all__ = ["exact_utc", "stamp_html"]


def _to_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    text = value.strip()
    return text or None


def exact_utc(value: datetime | str | None) -> str:
    """Tooltip text: ``2026-06-11 05:25 UTC``. Naive input is read as UTC
    (the repo's storage convention); aware input is converted. Unparseable
    strings pass through unchanged so the tooltip never lies."""
    iso = _to_iso(value)
    if iso is None:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def stamp_html(
    value: datetime | str | None,
    *,
    mode: str = "rel",
    css: str = "",
    include_year: bool = True,
    prefix: str = "",
) -> str:
    """One timestamp span, by the rules:
    ``<span class="fired-at" title="2026-06-11 05:25 UTC">2h ago</span>``.

    ``mode="rel"`` for activity, ``mode="date"`` for facts. ``css`` carries the
    surface's existing class so per-surface styling keeps working. ``prefix``
    renders inside the span before the value ("updated ", "applied ").
    Missing values render as an em-dash span (hide-don't-stub stays the
    caller's job; this is for cells that must show something).
    """
    iso = _to_iso(value)
    cls = f' class="{escape(css, quote=True)}"' if css else ""
    if iso is None:
        return f"<span{cls}>—</span>"
    label = fmt_reltime(iso) if mode == "rel" else fmt_date(iso, include_year=include_year)
    title = escape(exact_utc(iso), quote=True)
    return f'<span{cls} title="{title}">{escape(prefix + label)}</span>'

"""Closed document shell for offline projections using the canonical report master.

Consumers supply rendered content, never stylesheet overrides. Input renderers
remain responsible for escaping data in their markup.
"""

from __future__ import annotations

from html import escape

from report.renderers.workspace_styles import CSS


def render_offline_document(body_html: str, *, title: str) -> str:
    """Wrap a deterministic projection without scripts or external resources."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head><body>{body_html}</body></html>"""

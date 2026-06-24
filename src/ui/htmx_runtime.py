"""HTMX runtime loader — the vendored ``htmx.min.js`` inlined for the LIVE
command-center shell ONLY.

HTMX swaps server-rendered fragments, so it is useless in the offline ``file://``
report (no server) and would only add ~50KB of dead weight there. Unlike
``living_grid.head_assets`` (Alpine, used by both the shell AND the report),
this is wired into the command-center shell head exclusively — never into
``workspace_html``.

Self-contained (no asset fetches), verified free of ``</script>`` so it inlines
raw without escaping.
"""

from __future__ import annotations

from pathlib import Path

_HTMX_JS = (Path(__file__).resolve().parent / "vendor" / "htmx-2.0.4.min.js").read_text(
    encoding="utf-8"
)


def htmx_head() -> str:
    """The HTMX library inlined once for the shell ``<head>`` (live app only)."""
    return f"<script>{_HTMX_JS}</script>"

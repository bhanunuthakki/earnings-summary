"""HTMX runtime loader — the vendored ``htmx.min.js`` inlined for the LIVE
command-center shell ONLY.

HTMX swaps server-rendered fragments, so it is useless in the offline ``file://``
report (no server) and would only add ~50KB of dead weight there. Unlike
``living_grid.head_assets`` (Alpine, used by both the shell AND the report),
this is wired at the end of the command-center shell body exclusively — never
into
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
    """Legacy head markup for callers that explicitly require it.

    The command center uses :func:`htmx_body_assets` so HTMX does not compete
    with initial document parsing. This helper remains for compatible direct
    consumers; HTMX is never included in offline report assets.
    """
    return f"<script>{_HTMX_JS}</script>"


def htmx_body_assets() -> str:
    """Inline HTMX at body end for the live command center.

    It stays self-contained, while avoiding a synchronous framework parse in
    ``<head>``. By body end, HTMX can safely discover initial ``hx-*`` markup.
    """
    return f"<script>{_HTMX_JS}</script>"

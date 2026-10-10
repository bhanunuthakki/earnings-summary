"""The one render boundary for the command-center panel shell.

Every ``*_panel.py`` fragment renders through the same shell: an optional
family-style prefix, one ``<section class="panel">`` open (optionally with
extra classes/attributes), an optional ``<h2>`` title, the panel body, and the
section close. Before this module that shell was hand-copied into every panel
— 40+ modules each re-stating the same literal composition (the sprawl audit
counted 114 raw ``<section class="panel">`` literals across the panel family),
so a shell change meant an N-way edit and any divergence shipped silently.

:func:`panel_section` owns that composition; :func:`panel_empty` owns the
family's empty/degraded state. Panels keep owning their data, body markup, and
interaction hooks — this module owns only the shell.

Byte stability. This contract is a structural consolidation, not a visual
change: the emitted bytes are identical to the literal compositions it
replaces, pinned by characterization tests and the mission's panel-fragment
captures. ``title``, ``body``, ``message`` and every attribute fragment are
pre-rendered HTML (escape dynamic values at the call site) — the contract never
re-escapes, because the shapes it preserves already carry entity-escaped
literals (``Risk &amp; efficiency``).

Empty states. The design kit's ``k_empty`` is the empty/degraded primitive for
report and workspace surfaces; the command-center panel family still renders
its historical muted paragraph, and byte stability forbids changing that in a
structural pass. ``panel_empty`` centralizes the legacy shape so a future
sanctioned migration to ``k_empty`` (a reviewed golden update) happens in one
place instead of forty.

Design-language ownership. This module emits markup only — no CSS. It is
registered in ``ui.design_registry`` (owner ``design-system``); the shell's
visual vocabulary lives in the registered family style masters
(``pipeline/operations_styles.py``, ``pipeline/research_panel_styles.py``,
``pipeline/portfolio_styles.py``, ``pipeline/analysis_styles.py``) and the
shell's ``.panel`` rules. A shell visual change is a registry mutation, not a
per-panel edit.
"""

from __future__ import annotations

__all__ = ["panel_empty", "panel_section"]


def panel_section(
    *body: str,
    title: str = "",
    title_attrs: str = "",
    cls: str = "",
    attrs: str = "",
    style: str = "",
    tail: str = "",
) -> str:
    """Emit one panel shell: ``[style]<section class="panel[ cls]"[attrs]>
    [<h2[title_attrs]>title</h2>]body</section>[tail]``.

    Args:
        body: pre-rendered HTML fragments, joined in order inside the section.
        title: the panel's ``<h2>`` heading text (pre-rendered; pass ``""``
            when the nav or a header band already owns the label).
        title_attrs: extra ``<h2>`` attribute fragment (leading space
            included), e.g. ``' title="..."'``.
        cls: extra section classes appended after ``panel``.
        attrs: extra section attribute fragment (leading space included),
            e.g. ``' data-plc-root'``.
        style: the family-style prefix emitted before the section.
        tail: markup emitted after the section close (e.g. a panel-local
            ``<script>``).
    """
    classes = f"panel {cls}" if cls else "panel"
    head = f'<section class="{classes}"{attrs}>'
    if title:
        head += f"<h2{title_attrs}>{title}</h2>"
    return f"{style}{head}{''.join(body)}</section>{tail}"


def panel_empty(
    message: str,
    *,
    title: str = "",
    cls: str = "",
    attrs: str = "",
    style: str = "",
    tail: str = "",
) -> str:
    """Emit the panel shell's empty/degraded state.

    The panel family's historical shape: the shell plus one muted paragraph
    carrying the state message. ``message`` is pre-rendered HTML (callers embed
    ``<code>`` run instructions and escape dynamic values at the call site).
    See the module docstring for the ``k_empty`` relationship.
    """
    return panel_section(
        f'<p class="muted">{message}</p>',
        title=title,
        cls=cls,
        attrs=attrs,
        style=style,
        tail=tail,
    )

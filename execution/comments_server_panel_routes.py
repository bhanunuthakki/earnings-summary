"""The ``/api/panel/*`` fragment routes: dispatcher, holding, notes drawer."""

from __future__ import annotations

from comments_server_context import ServerContext
from comments_server_panel_builders import PANEL_BUILDERS
from flask import Flask, Response, abort, request

from pipeline.analytical_dashboard import build_analytical_dashboard
from pipeline.ticker_command_center import (
    render_holding_fragment,
    render_holding_picker_band,
    render_notes_drawer_fragment,
)


def register_panel_routes(app: Flask, ctx: ServerContext) -> None:
    db_path = ctx.db_path
    repo_root = ctx.repo_root

    @app.route("/api/panel/<name>", methods=["GET"])
    def panel_fragment(name: str):
        """One analytical panel as a head/foot-less HTML fragment, for the lazy
        command-center shell — builds only that panel's section. ``?ticker=``
        scopes the dropdown-driven panels (prereads, insiders) to one name.
        404 for an unknown panel."""
        builder = PANEL_BUILDERS.get(name)
        if builder is not None:
            return builder(ctx)
        from pipeline.analytical_dashboard_html import (
            PANEL_TO_SECTION,
            render_panel_fragment,
        )

        section_key = PANEL_TO_SECTION.get(name)
        if section_key is None:
            abort(404)
        ticker = request.args.get("ticker") or None
        dash = build_analytical_dashboard(db_path, sections={section_key}, ticker=ticker)
        fragment = render_panel_fragment(dash, name)
        if fragment is None:
            abort(404)
        return Response(fragment, mimetype="text/html")

    @app.route("/api/panel/holding", methods=["GET"])
    def holding_panel_fragment():
        """The per-holding drill-down as a head/foot-less fragment for the shell's
        Holding tab: a one-line utility band (search combobox · verdict · freshness ·
        report/DCF links · Ops/Notes icons) above the embedded ``/reports/<t>``
        iframe that carries the inline comment/chat/apply pipeline. With no
        ``?ticker=`` it returns the combobox band alone (UX9c) — the search picker
        is always present, including before any holding is opened."""
        ticker = request.args.get("ticker")
        if not ticker:
            return Response(render_holding_picker_band(repo_root), mimetype="text/html")
        # The renderer reads the application's resolved runtime DB path — the
        # same one this app's other panel routes use — never the forbidden
        # checkout-default data/portfolio.db location (M2 audit F11).
        return Response(
            render_holding_fragment(repo_root, ticker, db_path=db_path), mimetype="text/html"
        )

    @app.route("/api/panel/notes_drawer", methods=["GET"])
    def notes_drawer_panel_fragment():
        """The shell's shared ✎ Notes drawer (UX9b) as a fragment: quick-add
        (POSTs to /api/notes) above the open-notes list. ``?ticker=`` scopes
        it to one name (the Holding tab supplies its selection) and adds that
        name's recent alerts; without it, the newest open notes book-wide."""
        return Response(
            render_notes_drawer_fragment(repo_root, request.args.get("ticker")),
            mimetype="text/html",
        )

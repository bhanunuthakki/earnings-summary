"""Retired report-chat compatibility tombstones."""

from __future__ import annotations

from comments_server_context import ServerContext
from flask import Flask


def register_chat_routes(app: Flask, ctx: ServerContext) -> None:
    legacy_chat_migrated = ctx.legacy_chat_migrated

    @app.route("/chat/<ticker>", methods=["OPTIONS"])
    def chat_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>", methods=["GET"])
    def list_chat_endpoint(ticker: str):
        return (legacy_chat_migrated(ticker), 410)

    @app.route("/chat/<ticker>", methods=["POST"])
    def chat_endpoint(ticker: str):
        return (legacy_chat_migrated(ticker), 410)

    # ----- APPLY (Phase 4) -----

    @app.route("/chat/<ticker>/apply", methods=["OPTIONS"])
    def chat_apply_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>/apply", methods=["POST"])
    def chat_apply_endpoint(ticker: str):
        return (legacy_chat_migrated(ticker), 410)

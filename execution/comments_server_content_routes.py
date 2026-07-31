"""Read-only source, peek, ticker, report, and DCF content routes.

This module deliberately registers handlers directly on the parent Flask app.
That preserves the historical endpoint names and URL contracts while keeping
``comments_server.create_app`` responsible for lifecycle hooks and shared
state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from flask import Flask, Response, abort, redirect, request, send_file


class _TickerCommandCenter(Protocol):
    def to_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class ContentRouteContext:
    """Small, late-bound dependency set for the read-only content routes."""

    repo_root: Path
    db_path: Path
    open_db: Callable[[], sqlite3.Connection]
    safe_ticker: Callable[[str], str]
    build_ticker_command_center: Callable[[Path, str], _TickerCommandCenter]
    linked_gsheet: Callable[[Path, str], tuple[str | None, str | None]]
    default_user_id: str


def _parse_bbox_param(raw: str | None) -> tuple[float, float, float, float] | None:
    """Decode a PDF highlight bbox, tolerating malformed optional input."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(part) for part in parts)
    except ValueError:
        return None
    return (x0, y0, x1, y1)


def register_content_routes(app: Flask, context: ContentRouteContext) -> None:
    """Register read-only content routes without changing Flask contracts."""
    repo_root = context.repo_root
    db_path = context.db_path

    @app.route("/source/<int:doc_id>", methods=["GET"])
    def source_viewer(doc_id: int):
        """Dispatch one document to its in-app source viewer."""
        from pipeline.source_viewers import (
            load_document,
            render_fallback_page,
            render_form10k_page,
            render_pdf_page_view,
            render_statement_json_page,
            render_transcript_page,
        )

        fragment = request.args.get("fragment") == "1"
        html = render_transcript_page(repo_root, db_path, doc_id, fragment=fragment)
        if html is None:
            html = render_form10k_page(
                repo_root,
                db_path,
                doc_id,
                request.args.get("section"),
                fragment=fragment,
            )
        if html is None:
            html = render_statement_json_page(
                repo_root,
                db_path,
                doc_id,
                json_path=request.args.get("json_path"),
                row_label=request.args.get("row_label"),
                column_header=request.args.get("column_header"),
                fragment=fragment,
            )
        if html is None:
            html = render_pdf_page_view(
                repo_root,
                db_path,
                doc_id,
                request.args.get("page", type=int),
                bbox=_parse_bbox_param(request.args.get("bbox")),
                fragment=fragment,
            )
        if html is not None:
            return Response(html, mimetype="text/html")
        doc = load_document(db_path, doc_id)
        if (
            not fragment
            and doc is not None
            and doc.source_url
            and doc.source_url.startswith(("http://", "https://"))
        ):
            return redirect(doc.source_url, code=302)
        return Response(
            render_fallback_page(db_path, doc_id, fragment=fragment),
            mimetype="text/html",
        )

    @app.route("/source/<int:doc_id>/page/<int:page>.png", methods=["GET"])
    def source_pdf_page_image(doc_id: int, page: int):
        """Serve one lazily rendered, content-addressed PDF page image."""
        from pipeline.pdf_render import render_page_image
        from pipeline.source_viewers import load_document

        doc = load_document(db_path, doc_id)
        if doc is None or not doc.file_path.lower().endswith(".pdf") or not doc.sha256:
            abort(404)
        pdf_path = Path(doc.file_path)
        if not pdf_path.is_absolute():
            pdf_path = repo_root / pdf_path
        png = render_page_image(
            repo_root,
            pdf_path=pdf_path,
            sha256=doc.sha256,
            page=page,
        )
        if png is None:
            abort(404)
        return send_file(png, mimetype="image/png")

    @app.route("/api/peek/alert/<int:alert_id>", methods=["GET"])
    def peek_alert(alert_id: int):
        from pipeline.peeks import render_alert_peek

        html = render_alert_peek(db_path, alert_id)
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/alerts", methods=["GET"])
    def peek_alerts():
        from pipeline.peeks import render_alerts_list_peek

        return Response(
            render_alerts_list_peek(
                db_path,
                user_id=context.default_user_id,
                ticker=request.args.get("ticker") or None,
                status=request.args.get("status") or None,
            ),
            mimetype="text/html",
        )

    @app.route("/api/peek/ticker/<ticker>", methods=["GET"])
    def peek_ticker(ticker: str):
        from pipeline.peeks import render_ticker_peek

        conn = context.open_db()
        try:
            html = render_ticker_peek(conn, repo_root, ticker)
        finally:
            conn.close()
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/memo/<kind>", methods=["GET"])
    def peek_memo(kind: str):
        from pipeline.peeks import render_memo_peek

        html = render_memo_peek(db_path, kind)
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/review/<ticker>", methods=["GET"])
    def peek_review(ticker: str):
        from pipeline.peeks import render_review_peek

        return Response(
            render_review_peek(repo_root, db_path, ticker),
            mimetype="text/html",
        )

    @app.route("/api/peek/provenance", methods=["GET"])
    def peek_provenance():
        from pipeline.peeks import render_provenance_peek

        return Response(
            render_provenance_peek(db_path, request.args.get("ticker") or None),
            mimetype="text/html",
        )

    @app.route("/api/peek/documents", methods=["GET"])
    def peek_documents():
        from pipeline.peeks import render_new_docs_peek

        return Response(
            render_new_docs_peek(
                db_path,
                ticker=request.args.get("ticker") or "",
            ),
            mimetype="text/html",
        )

    @app.route("/api/peek/score", methods=["GET"])
    def peek_score():
        from pipeline.peeks import render_score_peek

        conn = context.open_db()
        try:
            html = render_score_peek(
                conn,
                repo_root,
                request.args.get("ticker") or "",
            )
        finally:
            conn.close()
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/earnings-prep", methods=["GET"])
    def peek_earnings_prep():
        from pipeline.peeks import render_earnings_prep_peek

        html = render_earnings_prep_peek(
            db_path,
            repo_root,
            request.args.get("ticker") or "",
        )
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/earnings-readout", methods=["GET"])
    def peek_earnings_readout():
        from pipeline.peeks import render_earnings_readout_peek

        html = render_earnings_readout_peek(
            db_path,
            repo_root,
            request.args.get("ticker") or "",
        )
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/fit", methods=["GET"])
    def peek_fit():
        from pipeline.peeks import render_fit_peek

        html = render_fit_peek(repo_root, request.args.get("ticker") or "")
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/whatif", methods=["GET"])
    def peek_whatif():
        from pipeline.peeks import render_what_if_peek

        try:
            ticker = context.safe_ticker(request.args.get("ticker") or "")
        except ValueError:
            abort(404)
        try:
            weight = float(request.args.get("w") or 0.03)
        except (TypeError, ValueError):
            weight = 0.03
        funding_param = (request.args.get("funding") or "new_cash").strip().lower()
        funding_mode = "pro_rata_reallocation" if funding_param == "pro_rata" else "new_cash"
        html = render_what_if_peek(
            repo_root,
            ticker,
            weight,
            funding_mode=funding_mode,
            db_path=db_path,
        )
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/etf_workup", methods=["GET"])
    def peek_etf_workup():
        from pipeline.etf_workup import render_etf_workup

        try:
            ticker = context.safe_ticker(request.args.get("ticker") or "")
        except ValueError:
            abort(404)
        conn = context.open_db()
        try:
            html = render_etf_workup(conn, repo_root, db_path, ticker)
        finally:
            conn.close()
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/discovery-compare", methods=["GET"])
    def peek_discovery_compare():
        from pipeline.peeks import render_discovery_compare_peek

        raw = request.args.get("tickers") or ""
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        if not parts or len(parts) > 3:
            abort(404)
        try:
            tickers = [context.safe_ticker(part) for part in parts]
        except ValueError:
            abort(404)
        html = render_discovery_compare_peek(repo_root, db_path, tickers)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/provenance/<fact_ref>", methods=["GET"])
    def peek_fact_provenance(fact_ref: str):
        from pipeline.peeks import render_fact_provenance_peek

        html = render_fact_provenance_peek(db_path, repo_root, fact_ref)
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/ticker/<ticker>", methods=["GET"])
    def ticker_api(ticker: str):
        command_center = context.build_ticker_command_center(repo_root, ticker)
        return command_center.to_dict()

    @app.route("/ticker/<ticker>", methods=["GET"])
    def ticker_page(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        return redirect(f"/#holding={validated}")

    @app.route("/reports/<ticker>", methods=["GET"])
    def latest_report_for_ticker(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        research_dir = repo_root / "output" / "research" / validated
        if not research_dir.exists():
            abort(404)
        matches = sorted(research_dir.glob("*_workspace.html"))
        if not matches:
            abort(404)
        return send_file(matches[-1])

    @app.route("/dcf/<ticker>", methods=["GET"])
    def latest_dcf_for_ticker(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        _sheet_id, sheet_url = context.linked_gsheet(repo_root, validated)
        if sheet_url:
            return redirect(sheet_url, code=302)
        live = repo_root / "dcf" / f"{validated}.xlsx"
        if live.exists():
            return send_file(live)
        research_dir = repo_root / "output" / "research" / validated
        dated = sorted(research_dir.glob("*_dcf.xlsx")) if research_dir.exists() else []
        if not dated:
            abort(404)
        return send_file(dated[-1])

    @app.route("/api/tickers", methods=["GET"])
    def tickers_api():
        conn = context.open_db()
        try:
            rows = conn.execute(
                "SELECT ticker, name, list_type FROM tracked_companies "
                "WHERE archived_at IS NULL ORDER BY list_type, ticker"
            ).fetchall()
        finally:
            conn.close()
        return {
            "tickers": [
                {
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "list_type": row["list_type"],
                }
                for row in rows
            ]
        }

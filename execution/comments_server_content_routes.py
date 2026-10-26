"""Read-only source, peek, ticker, report, and DCF content routes.

This module deliberately registers handlers directly on the parent Flask app.
That preserves the historical endpoint names and URL contracts while keeping
``comments_server.create_app`` responsible for lifecycle hooks and shared
state.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode

from comments_server_evaluation_projection import resolve_work_os_evaluation_item
from flask import (
    Flask,
    Response,
    abort,
    redirect,
    request,
    send_file,
)

from integrations.portfolio_tracker_client import LivePortfolio
from pipeline.work_os_briefs import (
    build_brief_descriptor,
    build_brief_library,
    load_report_reader_payload,
    resolve_report_artifact,
    resolve_working_draft,
)
from pipeline.work_os_company import build_company_desk
from pipeline.work_os_decisions import build_decision_projection
from provenance.evidence_native_candidates import (
    read_verified_local_evidence_bytes,
    select_evidence_native_candidates_by_id,
)
from provenance.source_read_context import SourceReadContext
from report.artifacts import (
    ReportPublicationConflictError,
    begin_or_reuse_report_request,
    build_report_sync_status,
    claim_drifted_report_request,
    complete_report_request,
    latest_report_artifacts_for_tickers,
    load_report_artifact_index,
    load_report_candidate_review,
    load_report_capture_receipt,
    load_report_file_capture_receipt,
    publish_report_candidate,
    resolve_report_model_binding,
)
from report.file_capture import replay_file_capture
from report.generation_context import ReportModelBinding
from report.read_capture import CaptureLimits, replay_read_capture


class _TickerCommandCenter(Protocol):
    def to_dict(self) -> dict[str, object]: ...


# Self-hosted webfonts vendored under src/ui/vendor/fonts/, beside the inlined
# Alpine asset. The Work OS shell's @font-face rules reference these exact
# files, so first paint never depends on an external font origin.
_VENDOR_FONTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ui" / "vendor" / "fonts"


@dataclass(frozen=True)
class ContentRouteContext:
    """Small, late-bound dependency set for the read-only content routes."""

    repo_root: Path
    db_path: Path
    open_db: Callable[[], sqlite3.Connection]
    get_read_db: Callable[[], sqlite3.Connection]
    safe_ticker: Callable[[str], str]
    build_ticker_command_center: Callable[[Path, str], _TickerCommandCenter]
    linked_gsheet: Callable[[Path, str], tuple[str | None, str | None]]
    probe_tracker: Callable[[], tuple[bool, str]]
    fetch_live_portfolio: Callable[[], LivePortfolio]
    default_user_id: str
    source_context: SourceReadContext | None = None


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


def _pinned_ocr_pdf_bytes(
    context: ContentRouteContext, doc_id: int, version_id: str, source_sha256: str
) -> bytes:
    authority = context.source_context
    if authority is None or authority.ocr_state_root is None:
        raise ValueError("OCR PDF source authority is unavailable")
    if len(version_id) > 128 or len(source_sha256) != 64:
        raise ValueError("OCR PDF source identity is invalid")
    conn = context.get_read_db()
    row = conn.execute(
        "SELECT version.blob_sha256,document.sha256 FROM evidence_document_versions version "
        "JOIN documents document ON document.id=version.legacy_document_id "
        "WHERE version.document_version_id=? AND version.legacy_document_id=? "
        "AND version.document_type='ir_supplement'",
        (version_id, doc_id),
    ).fetchone()
    if row is None or row[0] != source_sha256 or row[1] != source_sha256:
        raise ValueError("OCR PDF source document identity changed")
    candidates = select_evidence_native_candidates_by_id(
        conn, document_version_ids=(version_id,), include_legacy=True
    )
    if len(candidates) != 1 or candidates[0].media_type != "application/pdf":
        raise ValueError("OCR PDF source bytes are unavailable")
    candidate = candidates[0]
    if (
        candidate.blob_sha256 != source_sha256
        or candidate.byte_size > authority.max_document_bytes
        or candidate.byte_size > authority.max_total_bytes
    ):
        raise ValueError("OCR PDF source byte bound changed")
    return read_verified_local_evidence_bytes(
        conn,
        storage_uri=candidate.storage_uri,
        expected_sha256=source_sha256,
        expected_byte_size=candidate.byte_size,
        allowed_roots=authority.content_roots,
        document_version_id=version_id,
    ).raw_bytes


def register_content_routes(app: Flask, context: ContentRouteContext) -> None:
    from comments_server_financial_evidence_routes import register_growth_evidence_routes

    register_growth_evidence_routes(
        app,
        get_read_db=context.get_read_db,
        safe_ticker=context.safe_ticker,
        source_context=context.source_context,
    )
    """Register read-only content routes without changing Flask contracts."""
    repo_root = context.repo_root
    db_path = context.db_path

    def _evaluation_item(raw_ticker: str):
        """Resolve one item through the same no-cache projection as Evaluation."""
        try:
            ticker = context.safe_ticker(raw_ticker)
        except ValueError:
            return None
        conn = context.get_read_db()
        try:
            return resolve_work_os_evaluation_item(
                conn,
                repo_root,
                ticker,
                safe_ticker=context.safe_ticker,
            )
        except (OSError, ValueError, sqlite3.Error):
            return None

    def vendored_font_file(filename: str) -> Any:
        """Serve one self-hosted, content-stable webfont binary."""
        # The vendored font directory is flat: the default converter already
        # refuses slashes, the suffix check refuses non-woff2 names, and
        # send_file receives a path below the fixed vendor directory.
        if not filename.endswith(".woff2") or "\\" in filename:
            abort(404)
        font_path = _VENDOR_FONTS_DIR / filename
        if not font_path.is_file():
            abort(404)
        response = cast(
            Any,
            send_file(
                font_path,
                mimetype="font/woff2",
                conditional=True,
            ),
        )
        # Vendored font files are content-stable (see src/ui/vendor/fonts/
        # README.md): bytes change only on a deliberate re-vendor, so the
        # immutable directive keeps repeat shell loads off the network path.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    app.add_url_rule(
        "/src/ui/vendor/fonts/<filename>",
        "vendored_font_file",
        vendored_font_file,
        methods=["GET"],
    )

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

        version_id = request.args.get("version")
        source_sha256 = request.args.get("sha256")
        if version_id is not None or source_sha256 is not None:
            if version_id is None or source_sha256 is None:
                abort(404)
            try:
                pinned_bytes = _pinned_ocr_pdf_bytes(context, doc_id, version_id, source_sha256)
                pinned_query = urlencode({"version": version_id, "sha256": source_sha256})
                pinned_html = render_pdf_page_view(
                    repo_root,
                    db_path,
                    doc_id,
                    request.args.get("page", type=int),
                    bbox=_parse_bbox_param(request.args.get("bbox")),
                    fragment=request.args.get("fragment") == "1",
                    raw_bytes=pinned_bytes,
                    pinned_query=pinned_query,
                )
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                abort(404)
            if pinned_html is None:
                abort(404)
            return Response(
                pinned_html, mimetype="text/html", headers={"Cache-Control": "no-store"}
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

    @app.route("/source/version/<document_version_id>", methods=["GET"])
    def source_version_viewer(document_version_id: str):
        """Resolve one sealed evidence citation to its provenance chain.

        The sealed Ask lane cites ``/source/version/<document_version_id>?node=…``
        (``src/ask/sealed_retrieval.py``); this is that destination. An unknown
        document version, or a ``node`` that is not part of it, 404s honestly
        rather than rendering a fabricated chain.
        """
        from pipeline.source_viewers import render_document_version_page

        html = render_document_version_page(
            db_path,
            document_version_id,
            node_id=request.args.get("node"),
            fragment=request.args.get("fragment") == "1",
        )
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/source/<int:doc_id>/page/<int:page>.png", methods=["GET"])
    def source_pdf_page_image(doc_id: int, page: int):
        """Serve one lazily rendered, content-addressed PDF page image."""
        from pipeline.pdf_render import render_page_image, render_verified_page_bytes
        from pipeline.source_viewers import load_document

        version_id = request.args.get("version")
        source_sha256 = request.args.get("sha256")
        if version_id is not None or source_sha256 is not None:
            if version_id is None or source_sha256 is None:
                abort(404)
            try:
                pinned_bytes = _pinned_ocr_pdf_bytes(context, doc_id, version_id, source_sha256)
                rendered = render_verified_page_bytes(pinned_bytes, page=page)
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                abort(404)
            if rendered is None:
                abort(404)
            return Response(rendered, mimetype="image/png", headers={"Cache-Control": "no-store"})

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

    @app.route("/api/peek/news-events", methods=["GET"])
    def peek_news_events():
        from pipeline.peeks import render_news_events_peek

        return Response(
            render_news_events_peek(
                db_path, request.args.get("ticker") or "", conn=context.get_read_db()
            ),
            mimetype="text/html",
        )

    @app.route("/api/peek/ticker/<ticker>", methods=["GET"])
    def peek_ticker(ticker: str):
        from pipeline.peeks import render_ticker_peek

        html = render_ticker_peek(context.get_read_db(), repo_root, ticker)
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/memo/<kind>", methods=["GET"])
    def peek_memo(kind: str):
        from pipeline.peeks import render_memo_peek

        html = render_memo_peek(db_path, kind, conn=context.get_read_db())
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
            render_provenance_peek(
                db_path, request.args.get("ticker") or None, conn=context.get_read_db()
            ),
            mimetype="text/html",
        )

    @app.route("/api/peek/documents", methods=["GET"])
    def peek_documents():
        from pipeline.peeks import render_new_docs_peek

        return Response(
            render_new_docs_peek(
                db_path,
                ticker=request.args.get("ticker") or "",
                conn=context.get_read_db(),
            ),
            mimetype="text/html",
        )

    @app.route("/api/peek/earnings-prep", methods=["GET"])
    def peek_earnings_prep():
        from pipeline.peeks import render_earnings_prep_peek

        raw_artifact_id = request.args.get("artifact_id")
        artifact_id: int | None = None
        if raw_artifact_id is not None:
            try:
                artifact_id = int(raw_artifact_id)
            except ValueError:
                abort(400)
            if artifact_id <= 0:
                abort(400)
        html = render_earnings_prep_peek(
            db_path,
            repo_root,
            request.args.get("ticker") or "",
            artifact_id=artifact_id,
            conn=context.get_read_db(),
        )
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/earnings-readout", methods=["GET"])
    def peek_earnings_readout():
        from pipeline.peeks import render_earnings_readout_peek

        raw_artifact_id = request.args.get("artifact_id")
        artifact_id: int | None = None
        if raw_artifact_id is not None:
            try:
                artifact_id = int(raw_artifact_id)
            except ValueError:
                abort(400)
            if artifact_id <= 0:
                abort(400)
        html = render_earnings_readout_peek(
            db_path,
            repo_root,
            request.args.get("ticker") or "",
            artifact_id=artifact_id,
            conn=context.get_read_db(),
        )
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/earnings-draft", methods=["GET"])
    def peek_earnings_draft():
        from ui.prose import render_prose

        path = resolve_working_draft(repo_root, request.args.get("artifact_id") or "")
        if path is None:
            abort(404)
        try:
            body_md = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            abort(404)
        response = Response(render_prose(body_md), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/peek/investment-profile", methods=["GET"])
    def peek_investment_profile():
        from pipeline.peeks import render_investment_profile_peek

        item = _evaluation_item(request.args.get("ticker") or "")
        html = render_investment_profile_peek(item)
        if html is None:
            abort(404)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/peek/portfolio-impact", methods=["GET"])
    def peek_portfolio_impact():
        from pipeline.peeks import render_portfolio_impact_peek

        item = _evaluation_item(request.args.get("ticker") or "")
        html = render_portfolio_impact_peek(item)
        if html is None:
            abort(404)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/peek/weekly-packet", methods=["GET"])
    def peek_weekly_packet():
        """Task 3 (wave3b): the read-only quick-look behind the Home
        open-loops band's Sunday-packet line — no web reply surface exists
        (verdicts are given on Telegram), so the doorway peeks the packet
        state instead of navigating anywhere. Always 200s; never raises."""
        from pipeline.open_loops import render_weekly_packet_peek

        return Response(render_weekly_packet_peek(db_path), mimetype="text/html")

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
        html = render_etf_workup(context.get_read_db(), repo_root, db_path, ticker)
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

        html = render_fact_provenance_peek(db_path, repo_root, fact_ref, conn=context.get_read_db())
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/canonical-financial", methods=["GET"])
    def peek_canonical_financial():
        from pipeline.peeks import ocr_pdf_document_id, render_canonical_financial_peek
        from pipeline.source_viewers import render_record_view
        from sources.report_financials import FinancialEvidenceReference, read_financial_evidence

        values = request.args.getlist("reference")
        if (
            not set(request.args) <= {"reference", "fragment"}
            or len(values) != 1
            or len(values[0]) > 4096
            or ("fragment" in request.args and request.args.getlist("fragment") != ["1"])
        ):
            return Response("Invalid evidence reference.", status=400, mimetype="text/html")
        try:
            reference = FinancialEvidenceReference.model_validate_json(values[0])
            if context.safe_ticker(reference.ticker) != reference.ticker:
                raise ValueError("invalid ticker")
        except ValueError:
            return Response("Invalid evidence reference.", status=400, mimetype="text/html")
        # The reference selects its complete table or series admission owner.
        # Both preserve the request read snapshot and perform no writes.
        try:
            conn = context.get_read_db()
            cell = read_financial_evidence(conn, reference, source_context=context.source_context)
            source_doc_id = ocr_pdf_document_id(conn, cell) if cell is not None else None
        except (OSError, RuntimeError, sqlite3.Error):
            cell = None
            source_doc_id = None
        if cell is None:
            return Response(
                render_record_view(
                    "Evidence unavailable",
                    "Evidence unavailable for this selection.",
                    fragment=request.args.get("fragment") == "1",
                ),
                status=404,
                mimetype="text/html",
            )
        response = Response(
            render_record_view(
                "Evidence for the selected value",
                render_canonical_financial_peek(cell, reference, ocr_pdf_document_id=source_doc_id),
                fragment=request.args.get("fragment") == "1",
            ),
            mimetype="text/html",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

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
        return redirect(f"/?screen=company-desk&ticker={validated}")

    @app.route("/reports/<ticker>", methods=["GET"])
    def latest_report_for_ticker(ticker: str):
        artifact = resolve_report_artifact(repo_root, ticker)
        if artifact is not None:
            if artifact.reader_mode == "shared_body" and request.args.get("standalone") != "1":
                return redirect(
                    "/?"
                    + urlencode(
                        {
                            "ticker": artifact.ticker,
                            "work_os_brief": artifact.ticker,
                            "work_os_brief_artifact": artifact.artifact_id,
                        }
                    )
                    + "#screen-workspace"
                )
            artifact_path = repo_root / artifact.standalone_path
            if not artifact_path.is_file():
                abort(404)
            return send_file(artifact_path)
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        artifact_id = request.args.get("artifact_id")
        if artifact_id:
            artifact = resolve_report_artifact(repo_root, artifact_id)
            if artifact is None or artifact.ticker != validated:
                abort(404)
            artifact_path = repo_root / artifact.standalone_path
            if not artifact_path.is_file():
                abort(404)
            if artifact.reader_mode == "shared_body" and request.args.get("standalone") != "1":
                return redirect(
                    "/?"
                    + urlencode(
                        {
                            "ticker": validated,
                            "work_os_brief": validated,
                            "work_os_brief_artifact": artifact.artifact_id,
                        }
                    )
                    + "#screen-workspace"
                )
            return send_file(artifact_path)
        artifact = latest_report_artifacts_for_tickers(repo_root, (validated,)).get(validated)
        if artifact is not None and (repo_root / artifact.standalone_path).is_file():
            artifact_path = repo_root / artifact.standalone_path
            if not artifact_path.is_file():
                abort(404)
            if artifact.reader_mode == "shared_body" and request.args.get("standalone") != "1":
                return redirect(
                    "/?"
                    + urlencode(
                        {
                            "ticker": validated,
                            "work_os_brief": validated,
                            "work_os_brief_artifact": artifact.artifact_id,
                        }
                    )
                    + "#screen-workspace"
                )
            return send_file(artifact_path)
        research_dir = repo_root / "output" / "research" / validated
        if not research_dir.exists():
            abort(404)
        matches = sorted(research_dir.glob("*_workspace.html"))
        if not matches:
            abort(404)
        artifact = next(
            (
                item
                for item in load_report_artifact_index(repo_root).items
                if item.ticker == validated
                and item.workspace_sha256 == hashlib.sha256(matches[-1].read_bytes()).hexdigest()
            ),
            None,
        )
        if (
            artifact
            and artifact.reader_mode == "shared_body"
            and request.args.get("standalone") != "1"
        ):
            return redirect(
                "/?"
                + urlencode(
                    {
                        "ticker": validated,
                        "work_os_brief": validated,
                        "work_os_brief_artifact": artifact.artifact_id,
                    }
                )
                + "#screen-workspace"
            )
        return send_file(matches[-1])

    @app.route("/api/report-sync/<ticker>", methods=["GET"])
    def report_sync_status(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        status = build_report_sync_status(
            repo_root,
            validated,
            conn=context.get_read_db(),
            candidate_artifact_id=request.args.get("candidate_artifact_id"),
        )
        etag = str(status["etag"])
        if request.headers.get("If-None-Match", "").strip('"') == etag:
            return Response(status=304, headers={"ETag": f'"{etag}"'})
        response = Response(json.dumps(status), mimetype="application/json")
        response.headers["ETag"] = f'"{etag}"'
        return response

    @app.route("/api/report-sync/<ticker>/candidate", methods=["POST"])
    def report_sync_candidate(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        raw_payload = request.get_json(silent=True)
        payload: dict[str, object] = (
            cast(dict[str, object], raw_payload) if isinstance(raw_payload, dict) else {}
        )
        if_match = request.headers.get("If-Match")
        if if_match:
            clean_if_match = if_match.strip('"')
            live = build_report_sync_status(repo_root, validated, conn=context.get_read_db())
            valid_etags = {str(live["etag"])}
            cand_id = (
                request.args.get("candidate_artifact_id")
                or payload.get("candidate_artifact_id")
                or payload.get("artifact_id")
            )
            if cand_id:
                live_cand = build_report_sync_status(
                    repo_root,
                    validated,
                    conn=context.get_read_db(),
                    candidate_artifact_id=str(cand_id),
                )
                valid_etags.add(str(live_cand["etag"]))
            for item in load_report_artifact_index(repo_root).items:
                if item.ticker == validated and item.publication_status == "draft_preview":
                    status_cand = build_report_sync_status(
                        repo_root,
                        validated,
                        conn=context.get_read_db(),
                        candidate_artifact_id=item.artifact_id,
                    )
                    valid_etags.add(str(status_cand["etag"]))
            if clean_if_match not in valid_etags:
                return Response(
                    json.dumps({"error": "etag_mismatch"}), status=412, mimetype="application/json"
                )
        owner_action_raw = request.headers.get("X-Owner-Action-Id") or payload.get(
            "owner_action_id"
        )
        owner_action_id = str(owner_action_raw) if owner_action_raw is not None else None
        expected_raw = request.args.get("expected_current_artifact_id") or payload.get(
            "expected_current_artifact_id"
        )
        expected_current = str(expected_raw) if expected_raw is not None else None

        binding = resolve_report_model_binding(repo_root, validated, conn=context.get_read_db())
        model_key = binding.row_id if isinstance(binding, ReportModelBinding) else "nomodel"
        semantic_key = f"candidate_refresh:{validated}:{model_key}:{expected_current or ''}"

        record, req_reused = begin_or_reuse_report_request(
            repo_root,
            semantic_request_key=semantic_key,
            kind="candidate_refresh",
            ticker=validated,
            owner_action_id=owner_action_id,
        )
        if req_reused:
            if record.status == "running":
                return Response(
                    json.dumps(
                        {
                            "status": "running",
                            "message": "candidate refresh in progress",
                            "ticker": validated,
                        }
                    ),
                    status=202,
                    mimetype="application/json",
                )
            if record.status == "completed" and record.result_payload:
                cand_id = record.artifact_id or str(record.result_payload.get("artifact_id") or "")
                is_valid = False
                if cand_id:
                    cand_capture = load_report_capture_receipt(repo_root, validated, cand_id)
                    cand_file = load_report_file_capture_receipt(repo_root, validated, cand_id)
                    cand_rev = load_report_candidate_review(repo_root, validated, cand_id)
                    if cand_capture is not None and cand_file is not None and cand_rev is not None:
                        try:
                            active_db = db_path
                            if active_db.is_file():
                                replay_read_capture(active_db, cand_capture, limits=CaptureLimits())
                                replay_file_capture(cand_file, repo_root)
                                is_valid = True
                        except Exception:
                            is_valid = False
                if is_valid:
                    cached_payload = dict(record.result_payload)
                    cached_payload["reused"] = True
                    cached_payload["status"] = "reused"
                    return Response(
                        json.dumps(cached_payload, default=str),
                        status=200,
                        mimetype="application/json",
                    )
                record, already_running = claim_drifted_report_request(
                    repo_root,
                    semantic_request_key=semantic_key,
                    ticker=validated,
                    owner_action_id=owner_action_id,
                )
                if already_running:
                    return Response(
                        json.dumps(
                            {
                                "status": "running",
                                "message": "candidate refresh in progress",
                                "ticker": validated,
                            }
                        ),
                        status=202,
                        mimetype="application/json",
                    )

        try:
            from execution.build_artifacts import build_candidate_artifact
        except ImportError:
            from build_artifacts import build_candidate_artifact

        try:
            result = build_candidate_artifact(
                validated,
                repo_root,
                db_path=db_path,
                source_context=context.source_context,
                expected_current_artifact_id=expected_current,
            )
            complete_report_request(
                repo_root,
                semantic_request_key=semantic_key,
                status="completed" if result.get("status") in ("ok", "reused") else "failed",
                artifact_id=str(result.get("artifact_id") or "") or None,
                result_payload=result,
            )
        except Exception:
            complete_report_request(
                repo_root,
                semantic_request_key=semantic_key,
                status="failed",
            )
            raise

        status_code = (
            409
            if result.get("status") == "capture_unavailable"
            else (200 if result.get("reused") else 201)
        )
        return Response(
            json.dumps(result, default=str), status=status_code, mimetype="application/json"
        )

    @app.route("/api/report-sync/<ticker>/publish", methods=["POST"])
    def report_sync_publish(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        raw_payload = request.get_json(silent=True)
        payload: dict[str, object] = (
            cast(dict[str, object], raw_payload) if isinstance(raw_payload, dict) else {}
        )
        artifact_id_val = str(payload.get("artifact_id") or "")
        expected_raw = payload.get("expected_current_artifact_id")
        expected_current = str(expected_raw) if expected_raw is not None else None
        owner_action_val = str(payload.get("owner_action_id") or "")
        if owner_action_val:
            index = load_report_artifact_index(repo_root)
            existing = next(
                (item for item in index.publications if item.owner_action_id == owner_action_val),
                None,
            )
            if existing is not None:
                if (existing.artifact_id, existing.expected_current_artifact_id) == (
                    artifact_id_val,
                    expected_current,
                ):
                    return Response(
                        existing.model_dump_json(), status=200, mimetype="application/json"
                    )
                return Response(
                    json.dumps({"error": "owner_action_id_conflict"}),
                    status=409,
                    mimetype="application/json",
                )
        if_match = request.headers.get("If-Match")
        if if_match:
            clean_if_match = if_match.strip('"')
            live_with_candidate = build_report_sync_status(
                repo_root,
                validated,
                conn=context.get_read_db(),
                candidate_artifact_id=artifact_id_val or None,
            )
            live_plain = build_report_sync_status(repo_root, validated, conn=context.get_read_db())
            candidate_etag = str(live_with_candidate.get("etag") or "")
            plain_etag = str(live_plain.get("etag") or "")
            if clean_if_match != candidate_etag and clean_if_match != plain_etag:
                return Response(
                    json.dumps({"error": "etag_mismatch"}), status=412, mimetype="application/json"
                )
        try:
            approval, reused = publish_report_candidate(
                repo_root,
                db_path=db_path,
                ticker=validated,
                artifact_id=artifact_id_val,
                expected_current_artifact_id=expected_current,
                owner_action_id=owner_action_val,
            )
        except ReportPublicationConflictError as exc:
            return Response(
                json.dumps({"error": exc.reason, "detail": exc.detail}),
                status=409,
                mimetype="application/json",
            )
        return Response(
            approval.model_dump_json(), status=200 if reused else 201, mimetype="application/json"
        )

    @app.route("/api/work-os/briefs/<artifact_id>", methods=["GET"])
    def brief_descriptor_api(artifact_id: str):
        artifact = resolve_report_artifact(repo_root, artifact_id)
        if artifact is None:
            abort(404)
        response = Response(
            build_brief_descriptor(
                repo_root, artifact, conn=context.get_read_db()
            ).model_dump_json(),
            mimetype="application/json",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/work-os/briefs", methods=["GET"])
    def brief_library_api():
        ticker = request.args.get("ticker")
        if ticker is not None:
            try:
                ticker = context.safe_ticker(ticker)
            except ValueError:
                abort(400)
        coverage_role = request.args.get("coverage_role")
        if coverage_role not in (None, "portfolio", "evaluation", "unknown"):
            return ({"error": "invalid coverage_role"}, 400)
        artifact_kind = request.args.get("artifact_kind")
        if artifact_kind not in (None, "full_brief", "pre_earnings", "post_earnings"):
            return ({"error": "invalid artifact_kind"}, 400)
        status = request.args.get("status")
        if status not in (None, "available", "degraded"):
            return ({"error": "invalid status"}, 400)
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            return ({"error": "limit must be an integer"}, 400)
        if not 1 <= limit <= 100:
            return ({"error": "limit must be between 1 and 100"}, 400)
        try:
            payload = build_brief_library(
                repo_root,
                conn=context.get_read_db(),
                ticker=ticker,
                artifact_kind=artifact_kind,
                coverage_role=coverage_role,
                status=status,
                cursor=request.args.get("cursor"),
                limit=limit,
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        response = Response(payload.model_dump_json(), mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/work-os/companies/<ticker>/desk", methods=["GET"])
    def company_desk_api(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        # The liveness probe and the live-portfolio fetch are independent
        # round-trips to the same tracker, so they overlap on a request-scoped
        # 2-worker pool: a slow-but-alive tracker costs ONE window instead of
        # probe-then-fetch (M2 audit F7), and the request still issues at most
        # ONE probe. The probe keeps gating the payload: when it fails
        # (connection refused or timed out) the response carries the exact
        # same prompt degraded envelope as before and never waits for the
        # in-flight fetch — the fetch result is discarded and its worker
        # drains on its own bounded per-call timeout.
        probe_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="desk-tracker"
        )
        try:
            probe_future = probe_pool.submit(context.probe_tracker)
            live_future = probe_pool.submit(context.fetch_live_portfolio)
            tracker_alive, tracker_url = probe_future.result()
            if tracker_alive:
                live = live_future.result()
            else:
                live = LivePortfolio(
                    available=False,
                    api_url=tracker_url,
                    error="tracker liveness probe failed (connection refused or timed out)",
                )
        finally:
            # wait=False: a failed probe must not block the response on the
            # still-running fetch (bounded by its own per-call timeout); on
            # the alive path both futures are already resolved.
            probe_pool.shutdown(wait=False, cancel_futures=True)
        try:
            payload = build_company_desk(
                repo_root,
                context.get_read_db(),
                validated,
                live=live,
            )
        except LookupError:
            abort(404)
        response = Response(payload.model_dump_json(), mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/work-os/briefs/<artifact_id>/body", methods=["GET"])
    def brief_body_api(artifact_id: str):
        artifact = resolve_report_artifact(repo_root, artifact_id)
        if artifact is None:
            abort(404)
        standalone_url = (
            f"/reports/{artifact.ticker}?artifact_id={artifact.artifact_id}&standalone=1"
        )
        if artifact.reader_mode != "shared_body" or artifact.body_path is None:
            return (
                {
                    "schema_version": "report_body_unavailable.v1",
                    "artifact_id": artifact.artifact_id,
                    "status": "legacy_standalone",
                    "standalone_url": standalone_url,
                },
                409,
            )
        try:
            decision, _conditions, _warnings = build_decision_projection(
                context.get_read_db(), artifact.ticker
            )
            payload = load_report_reader_payload(
                repo_root,
                artifact_id,
                decision=decision,
            )
        except FileNotFoundError:
            return (
                {
                    "schema_version": "report_body_unavailable.v1",
                    "artifact_id": artifact.artifact_id,
                    "status": "body_missing",
                    "standalone_url": standalone_url,
                },
                409,
            )
        except ValueError as exc:
            return (
                {
                    "schema_version": "report_body_unavailable.v1",
                    "artifact_id": artifact.artifact_id,
                    "status": str(exc),
                    "standalone_url": standalone_url,
                },
                409,
            )
        response = Response(payload.model_dump_json(), mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Report-Artifact-ID"] = artifact.artifact_id
        response.headers["X-Report-Body-SHA256"] = payload.body_sha256
        response.set_etag(payload.body_sha256, weak=False)
        return response

    @app.route("/api/work-os/report-reader.css", methods=["GET"])
    def report_reader_css():
        from report.renderers.workspace_reader_assets import READER_CSS

        response = Response(READER_CSS, mimetype="text/css")
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    @app.route("/dcf/<ticker>", methods=["GET"])
    def latest_dcf_for_ticker(ticker: str):
        try:
            validated = context.safe_ticker(ticker)
        except ValueError:
            abort(400)
        from dcf.availability import resolve_dcf_route_artifact

        artifact = resolve_dcf_route_artifact(repo_root, validated)
        if artifact is None:
            abort(404)
        if artifact.kind == "sheet":
            return redirect(artifact.target, code=302)
        return send_file(artifact.target)

    @app.route("/api/tickers", methods=["GET"])
    def tickers_api():
        rows = (
            context.get_read_db()
            .execute(
                "SELECT ticker, name, list_type FROM tracked_companies "
                "WHERE archived_at IS NULL ORDER BY list_type, ticker"
            )
            .fetchall()
        )
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

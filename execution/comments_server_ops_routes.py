"""Operations diagnostics, portfolio risk/positioning, and position lifecycle routes."""

from __future__ import annotations

import hashlib
import sqlite3
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import cast

from comments_server_context import ServerContext
from flask import Flask, Response, request
from pydantic import ValidationError

from identity import DEFAULT_USER_ID
from operations.host_runtime import build_host_bundle
from operations.kpi_semantic_review_export import (
    KPI_SEMANTIC_EXPORT_RELATIVE_ROOT,
    KpiSemanticReviewExportError,
    load_current_kpi_semantic_review_partition,
    load_current_kpi_semantic_review_ticker_manifest,
)
from operations.paths import scheduler_receipt_path, service_receipt_path
from operations.review_bundle import (
    build_operations_review_bundle,
    database_lineage_identity,
    load_kpi_repair_review,
)
from operations.snapshot import collect_operations_snapshot
from pipeline.kpi_semantic_scope import scoped_kpi_definitions
from runtime.secrets import secret_read_path
from server_runtime.access import private_mobile_origin


def register_ops_routes(app: Flask, ctx: ServerContext) -> None:
    client_error = ctx.client_error
    collect_current_readme_status = ctx.collect_current_readme_status
    db_path = ctx.db_path
    declared_operations = ctx.declared_operations
    get_operations_read_db = ctx.get_operations_read_db
    operations_review_code_identity = ctx.operations_review_code_identity
    repo_root = ctx.repo_root
    resolved_code_root = ctx.resolved_code_root

    @app.route("/api/portfolio/risk-matrix", methods=["GET"])
    def portfolio_risk_matrix_api():
        """Generation 3 Performance & Risk cross-asset correlation and factor exposure."""
        from get_portfolio_risk_matrix import load_portfolio_risk_matrix

        data = load_portfolio_risk_matrix(repo_root)
        response = app.json.response(data.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/positioning/simulate", methods=["POST", "OPTIONS"])
    def positioning_simulate_api():
        """Generation 3 pre-trade portfolio impact simulator."""
        if request.method == "OPTIONS":
            return ("", 204)
        from simulate_trade_impact import (
            SimulateTradeRequest,
            SimulationDataUnavailableError,
            simulate_trade_impact,
        )

        raw = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            simulation = SimulateTradeRequest.model_validate(raw)
        except ValidationError as exc:
            return ({"error": str(exc)}, 400)
        try:
            result = simulate_trade_impact(repo_root, simulation)
        except SimulationDataUnavailableError as exc:
            return ({"error": str(exc)}, 503)
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        response = app.json.response(result.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/portfolio/policy", methods=["PUT", "OPTIONS"])
    def replace_portfolio_policy_api():
        """Proxy one explicit owner-approved policy replacement to the tracker.

        The companion tracker remains the only policy writer. This server does
        not cache, transform, or persist the submitted state: a caller receives
        either tracker-confirmed state or a stable failure category.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("X-Portfolio-Write-Intent") != "replace-policy":
            return client_error("explicit policy replacement intent required", 403)

        from integrations.portfolio_tracker_client import (
            PolicyReplacementRequest,
            replace_portfolio_policy,
        )

        try:
            replacement = PolicyReplacementRequest.model_validate(
                request.get_json(silent=True) or {}
            )
        except ValidationError:
            return client_error("invalid policy replacement request", 400)
        if replacement.source != "earnings_summary":
            return client_error("policy replacement source must be earnings_summary", 400)

        result = replace_portfolio_policy(replacement)
        if result.accepted or result.pending_recomputation:
            assert result.policy is not None
            response = app.json.response(
                {"status": result.status, "policy": result.policy.model_dump(mode="json")}
            )
            if result.pending_recomputation:
                response.status_code = 202
            return response
        status_codes = {
            "validation_error": 422,
            "revision_conflict": 409,
            "idempotency_conflict": 409,
            "unauthorized": 403,
            "offline": 503,
            "recomputation_failure": 503,
            "provider_error": 502,
            "malformed_response": 502,
        }
        payload: dict[str, object] = {"error": result.status}
        if result.current_revision is not None:
            payload["current_revision"] = result.current_revision
        return (payload, status_codes[result.status])

    @app.route("/api/operations/host-runtime", methods=["GET"])
    def operations_host_runtime_api():
        """Serve bounded cached host evidence without probing or recovery."""
        configured_private_origin = private_mobile_origin(
            config_path=secret_read_path("private_mobile_base_url", repo_root=repo_root)
        )
        if (
            configured_private_origin is None
            or urllib.parse.urlparse(configured_private_origin).scheme != "https"
        ):
            return client_error(
                "private review origin is not configured as HTTPS; refusing to emit an identity",
                503,
            )
        bundle = build_host_bundle(
            repo_root,
            datetime.now(UTC),
            configured_private_origin,
            operations_review_code_identity,
        )
        response = app.json.response(bundle.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"{bundle.content_sha256}"'
        return response

    @app.route("/api/operations/review-bundle", methods=["GET"])
    def operations_review_bundle_api():
        """Sanitized, typed Windows authority projection for Mac review."""
        configured_private_origin = private_mobile_origin(
            config_path=secret_read_path("private_mobile_base_url", repo_root=repo_root)
        )
        if (
            configured_private_origin is None
            or urllib.parse.urlparse(configured_private_origin).scheme != "https"
        ):
            return client_error(
                "private review origin is not configured as HTTPS; refusing to emit an identity",
                503,
            )
        operations_conn = get_operations_read_db()
        snapshot = collect_operations_snapshot(
            declared_operations,
            repo_root=repo_root,
            conn=operations_conn,
            observed_at=datetime.now(UTC),
            scheduler_receipt_path=scheduler_receipt_path(repo_root),
            service_receipt_path=service_receipt_path(repo_root),
        )
        semantic_rows = scoped_kpi_definitions(
            operations_conn,
            repo_root=resolved_code_root,
            user_id=DEFAULT_USER_ID,
        )
        try:
            database_instance = database_lineage_identity(operations_conn)
        except (sqlite3.Error, ValueError):
            # Keep the read-only review endpoint available during schema drift,
            # while making pin enrollment and Mac validation fail closed.
            database_instance = None
        bundle = build_operations_review_bundle(
            snapshot=snapshot,
            registry=declared_operations,
            semantic_rows=semantic_rows,
            serving_origin=configured_private_origin,
            code_instance=operations_review_code_identity,
            database_instance=database_instance,
            kpi_repair=load_kpi_repair_review(
                repo_root=repo_root,
                observed_at=snapshot.observed_at,
            ),
        )
        response = app.json.response(bundle.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"{bundle.content_sha256}"'
        return response

    @app.route("/api/operations/kpi-semantic-review/<ticker>", methods=["GET"])
    def operations_kpi_semantic_review_api(ticker: str):
        """Serve the bounded current partition manifest for one portfolio ticker."""
        try:
            manifest, payload = load_current_kpi_semantic_review_ticker_manifest(
                root=db_path.parent.parent / KPI_SEMANTIC_EXPORT_RELATIVE_ROOT,
                ticker=ticker,
                now=datetime.now(UTC),
                max_age=timedelta(minutes=20),
            )
        except KpiSemanticReviewExportError as exc:
            if str(exc) in {"ticker is invalid", "ticker is outside the current portfolio export"}:
                return client_error("semantic review artifact not found", 404)
            return client_error("semantic review artifact is unavailable", 503)
        expected_code_instance = hashlib.sha256(
            operations_review_code_identity.encode("utf-8")
        ).hexdigest()
        if manifest.code_instance_sha256 != expected_code_instance:
            return client_error("semantic review artifact authority is stale", 503)
        response = Response(payload, mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"{manifest.content_sha256}"'
        return response

    @app.route(
        "/api/operations/kpi-semantic-review/<ticker>/partitions/<content_sha256>",
        methods=["GET"],
    )
    def operations_kpi_semantic_review_partition_api(ticker: str, content_sha256: str):
        """Serve only an exact partition referenced by the current ticker manifest."""
        try:
            export, payload = load_current_kpi_semantic_review_partition(
                root=db_path.parent.parent / KPI_SEMANTIC_EXPORT_RELATIVE_ROOT,
                ticker=ticker,
                content_sha256=content_sha256,
                now=datetime.now(UTC),
                max_age=timedelta(minutes=20),
            )
        except KpiSemanticReviewExportError as exc:
            if str(exc) in {
                "ticker is invalid",
                "ticker is outside the current portfolio export",
                "semantic review partition hash is invalid",
                "semantic review partition is not referenced by the current ticker manifest",
            }:
                return client_error("semantic review partition not found", 404)
            return client_error("semantic review partition is unavailable", 503)
        expected_code_instance = hashlib.sha256(
            operations_review_code_identity.encode("utf-8")
        ).hexdigest()
        if export.code_instance_sha256 != expected_code_instance:
            return client_error("semantic review artifact authority is stale", 503)
        response = Response(payload, mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"{export.content_sha256}"'
        return response

    @app.route("/api/position-lifecycle/<ticker>", methods=["GET"])
    def position_lifecycle_fragment(ticker: str):
        """The holding page's position-lifecycle timeline as a standalone
        fragment (S5 PR2) — the grading form re-fetches this after a POST so
        the section refreshes in place without reloading the shell."""
        from pipeline.position_lifecycle_panel import render_position_lifecycle_section

        return Response(
            render_position_lifecycle_section(db_path, ticker, user_id=DEFAULT_USER_ID),
            mimetype="text/html",
        )

    @app.route("/api/position-entries/<int:entry_id>", methods=["POST", "OPTIONS"])
    def position_entry_grade(entry_id: int):
        """Write the analyst's post-exit grading onto one position_entries row:
        ``{exit_reason?, lessons?, outcome_vs_thesis?}``. Omitted keys are left
        untouched; empty strings clear a field. 400 on an unknown outcome
        label, 404 on a missing row."""
        if request.method == "OPTIONS":
            return ("", 204)
        from position_lifecycle import update_exit_fields

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})

        def _opt(key: str) -> str | None:
            value = payload.get(key)
            return str(value) if value is not None else None

        try:
            ok = update_exit_fields(
                db_path=db_path,
                entry_id=entry_id,
                exit_reason=_opt("exit_reason"),
                lessons=_opt("lessons"),
                outcome_vs_thesis=_opt("outcome_vs_thesis"),
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        if not ok:
            return ({"error": "position_entries unavailable (pre-0088 DB?)"}, 500)
        return {"id": entry_id, "ok": True}

    @app.route("/api/readme-governance/status", methods=["GET"])
    def readme_governance_status():
        """Return the bounded status projection, never candidate prose or paths."""

        status = collect_current_readme_status()
        return status.model_dump(mode="json")

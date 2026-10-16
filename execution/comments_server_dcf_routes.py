"""DCF read/recompute/mutation routes for the local comments server."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from flask import Blueprint, Flask, abort, g, request

import ticker_validation
from dcf import redesign as dcf_redesign
from dcf.grade_evidence import load_dcf_grade_evidence
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


@dataclass(frozen=True, slots=True)
class DcfRouteContext:
    """Stable dependencies for the cohesive DCF route cluster."""

    repo_root: Path
    db_path: Path
    linked_gsheet: Callable[[Path, str], tuple[str | None, str | None]]
    recompute_payload: Callable[[dcf_redesign.RedesignInputs], dict[str, object]]


def _driver_inputs(inp: dcf_redesign.RedesignInputs) -> dcf_redesign.RedesignInputs:
    """Normalize requests to the discount rate that a workbook save retains."""
    return replace(
        inp,
        wacc=dcf_redesign.derive_wacc(
            risk_free_rate=inp.risk_free_rate,
            beta=inp.beta,
            equity_risk_premium=inp.equity_risk_premium,
            country_risk_premium=inp.country_risk_premium,
            cost_of_debt=inp.cost_of_debt,
            tax_rate=inp.tax_rate,
            current_price=inp.current_price,
            diluted_shares_m=inp.diluted_shares_m,
            total_debt_m=inp.total_debt_m,
        ),
    )


def create_dcf_blueprint(context: DcfRouteContext) -> Blueprint:
    """Build the DCF route cluster without closing over the parent Flask app."""
    repo_root = context.repo_root
    db_path = context.db_path
    recompute_payload = context.recompute_payload
    blueprint = Blueprint("dcf", __name__)

    def get_read_db() -> sqlite3.Connection:
        """Reuse one read-only connection for the lifetime of this request."""
        if "request_read_db" not in g:
            conn = connect_sqlite(
                db_path,
                role=SQLiteConnectionRole.READ_ONLY,
                schema_preflight=True,
            )
            conn.row_factory = sqlite3.Row
            g.request_read_db = conn
        return g.request_read_db

    @blueprint.teardown_request
    def close_request_db(_exception: BaseException | None = None) -> None:
        db_conn = g.pop("request_read_db", None)
        if db_conn is not None:
            with suppress(Exception):
                db_conn.close()

    @blueprint.route("/api/dcf-sheet/<ticker>", methods=["GET"])
    def dcf_sheet_link(ticker: str):
        t = ticker.upper()
        sheet_id, url = context.linked_gsheet(repo_root, t)
        return {"ticker": t, "sheet_id": sheet_id, "url": url}

    @blueprint.route("/api/dcf/inputs/<ticker>", methods=["GET"])
    def dcf_inputs(ticker: str):
        t = ticker.upper()
        live = repo_root / "dcf" / f"{t}.xlsx"
        if not live.exists():
            abort(404)
        try:
            inp = dcf_redesign.read_inputs(live)
        except dcf_redesign.RedesignError as exc:
            return ({"error": str(exc)}, 422)
        if inp is None:
            return ({"error": "DCF inputs not found"}, 404)
        return {"ticker": t, "inputs": inp.to_dict()}

    @blueprint.route("/api/dcf/evidence/<ticker>", methods=["GET"])
    def dcf_grade_evidence(ticker: str):
        try:
            validated = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        payload = load_dcf_grade_evidence(get_read_db(), validated)
        return payload.model_dump(mode="json")

    @blueprint.route("/api/dcf/recompute", methods=["POST", "OPTIONS"])
    def dcf_recompute():
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        data = cast("dict[str, object]", body)
        mode = data.get("wacc_mode", "override")
        if not isinstance(mode, str) or mode not in {"drivers", "override"}:
            return ({"error": "wacc_mode must be 'drivers' or 'override'"}, 400)
        raw = data.get("inputs")
        if not isinstance(raw, dict):
            return ({"error": "body.inputs (a DCF assumption object) required"}, 400)
        try:
            inp = dcf_redesign.RedesignInputs.from_dict(cast("dict[str, object]", raw))
            if mode == "drivers":
                inp = _driver_inputs(inp)
        except dcf_redesign.RedesignError as exc:
            return ({"error": f"invalid inputs: {exc}"}, 400)
        try:
            return recompute_payload(inp)
        except dcf_redesign.RedesignError as exc:
            return ({"error": str(exc)}, 422)

    @blueprint.route("/api/dcf/save", methods=["POST", "OPTIONS"])
    def dcf_save():
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        data = cast("dict[str, object]", body)
        ticker = data.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            return ({"error": "body.ticker required"}, 400)
        raw = data.get("inputs")
        if not isinstance(raw, dict):
            return ({"error": "body.inputs (a DCF assumption object) required"}, 400)
        try:
            inp = dcf_redesign.RedesignInputs.from_dict(cast("dict[str, object]", raw))
            inp = _driver_inputs(inp)
        except dcf_redesign.RedesignError as exc:
            return ({"error": f"invalid inputs: {exc}"}, 400)
        try:
            recompute_payload(inp)
        except dcf_redesign.RedesignError as exc:
            return ({"error": str(exc)}, 422)

        import refresh_dcf

        try:
            t = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        result = refresh_dcf.apply_edits(t, repo_root, db_path, inp)
        if result.get("status") not in {"ok", "committed_cleanup_failed"}:
            reason = str(result.get("reason", "save failed"))
            code = 409 if "no redesigned workbook" in reason or reason == "dcf_writer_busy" else 500
            return ({"error": reason, "result": result}, code)
        saved_raw = result.get("inputs")
        saved_inp = (
            dcf_redesign.RedesignInputs.from_dict(cast("dict[str, object]", saved_raw))
            if isinstance(saved_raw, dict)
            else None
        )
        response_payload = recompute_payload(saved_inp) if saved_inp is not None else {}
        if saved_inp is not None:
            response_payload["inputs"] = saved_inp.to_dict()
        return {
            **response_payload,
            "saved": True,
            "result": result,
            "recovery_required": result.get("recovery_required", False),
            "cleanup_warning": result.get("cleanup_warning"),
        }

    return blueprint


def register_dcf_routes(app: Flask, context: DcfRouteContext) -> None:
    """Register the isolated DCF Blueprint on ``app``."""
    app.register_blueprint(create_dcf_blueprint(context))

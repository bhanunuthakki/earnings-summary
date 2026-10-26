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
from dcf import family_editor
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
    state_root: Path | None = None


def _family_field_error(exc: family_editor.FamilyFieldError) -> tuple[dict[str, object], int]:
    payload: dict[str, object] = {"error": str(exc), "reason": exc.reason}
    if exc.field is not None:
        payload["field"] = exc.field
    return payload, exc.http_status


def _resolve_route_family(repo_root: Path, ticker: str) -> str | None:
    """Positive family resolution for one ticker; ``None`` → the FCFF path.

    Resolves via the actual valuation model (``refresh_dcf._valuation_model``):
    ``platform_dcf`` → the NU family, ``meli_platform_sotp`` → the MELI family,
    ``fcff_dcf`` (with a supported redesigned workbook, or none yet to check)
    → the existing FCFF path byte-identical. Any other owner family
    (bank/holdco/fintech/onon/new/none/...) → typed ``family_unsupported``
    BEFORE any generic parse.
    """
    import refresh_dcf

    model, _suggestion = refresh_dcf.valuation_model(repo_root, ticker)
    if model == "platform_dcf":
        return family_editor.NU_FAMILY
    if model == "meli_platform_sotp":
        return family_editor.MELI_FAMILY
    workbook = repo_root / "dcf" / f"{ticker.upper()}.xlsx"
    if model == "fcff_dcf" and (
        not workbook.is_file() or dcf_redesign.is_redesign_format(workbook)
    ):
        return None
    raise family_editor.FamilyFieldError(
        "family_unsupported", field=ticker.upper(), http_status=409
    )


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
        try:
            family = _resolve_route_family(repo_root, t)
        except family_editor.FamilyFieldError as exc:
            return _family_field_error(exc)
        if family is not None:
            import refresh_dcf

            try:
                return refresh_dcf.family_inputs(t, repo_root, db_path, context.state_root)
            except family_editor.FamilyPointerInvalidError as exc:
                return ({"error": str(exc)}, 409)
            except family_editor.FamilyFieldError as exc:
                return _family_field_error(exc)
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
        family_ticker = data.get("ticker")
        if isinstance(family_ticker, str) and family_ticker.strip():
            import refresh_dcf

            try:
                t = ticker_validation.safe_ticker(family_ticker)
            except ValueError:
                return ({"error": "invalid ticker"}, 400)
            try:
                family = _resolve_route_family(repo_root, t)
            except family_editor.FamilyFieldError as exc:
                return _family_field_error(exc)
            if family is not None:
                body_family = data.get("family")
                if body_family is not None and body_family != family:
                    return (
                        {
                            "error": f"family_mismatch:{body_family}",
                            "reason": "family_mismatch",
                            "field": str(body_family),
                        },
                        409,
                    )
                raw_edits = data.get("inputs")
                if not isinstance(raw_edits, dict):
                    return (
                        {"error": "body.inputs (a family edit object) required"},
                        400,
                    )
                try:
                    return refresh_dcf.family_preview(
                        t,
                        repo_root,
                        db_path,
                        context.state_root,
                        cast("dict[str, object]", raw_edits),
                    )
                except family_editor.FamilyPointerInvalidError as exc:
                    return ({"error": str(exc)}, 409)
                except family_editor.FamilyFieldError as exc:
                    return _family_field_error(exc)
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
        import refresh_dcf

        try:
            t = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        try:
            family = _resolve_route_family(repo_root, t)
        except family_editor.FamilyFieldError as exc:
            return _family_field_error(exc)
        if family is not None:
            body_family = data.get("family")
            if body_family is not None and body_family != family:
                return (
                    {
                        "error": f"family_mismatch:{body_family}",
                        "reason": "family_mismatch",
                        "field": str(body_family),
                    },
                    409,
                )
            raw_edits = data.get("inputs")
            if not isinstance(raw_edits, dict):
                return ({"error": "body.inputs (a family edit object) required"}, 400)
            author = data.get("author")
            if not isinstance(author, str) or not author.strip():
                return ({"error": "body.author required"}, 400)
            rationale = data.get("rationale")
            if not isinstance(rationale, str) or len(rationale.strip()) < 20:
                return ({"error": "body.rationale (min 20 chars) required"}, 400)
            try:
                result = refresh_dcf.apply_family_edits(
                    t,
                    repo_root,
                    db_path,
                    context.state_root,
                    cast("dict[str, object]", raw_edits),
                    author=author.strip(),
                    rationale=rationale.strip(),
                )
            except (
                family_editor.FamilyProposalConflictError,
                family_editor.FamilyPointerInvalidError,
            ) as exc:
                return ({"error": str(exc)}, 409)
            except family_editor.FamilyFieldError as exc:
                return _family_field_error(exc)
            status = str(result.get("status"))
            if status in {"ok", "committed_cleanup_failed", "proposal_pending_review"}:
                return result
            if status == "blocked":
                reason = str(result.get("reason", "save failed"))
                return ({"error": reason, "result": result}, 409)
            reason = str(result.get("reason", "save failed"))
            code = (
                409 if reason in {"family_workbook_unavailable", "model_value_unavailable"} else 500
            )
            return ({"error": reason, "result": result}, code)
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
            "model_revision": result.get("model_revision"),
            "result": result,
            "recovery_required": result.get("recovery_required", False),
            "cleanup_warning": result.get("cleanup_warning"),
        }

    return blueprint


def register_dcf_routes(app: Flask, context: DcfRouteContext) -> None:
    """Register the isolated DCF Blueprint on ``app``."""
    app.register_blueprint(create_dcf_blueprint(context))

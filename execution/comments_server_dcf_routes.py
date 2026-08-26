"""DCF read/recompute/mutation routes for the local comments server."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
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


def _write_provenance_retry_receipt(
    repo_root: Path,
    *,
    ticker: str,
    field_key: str,
    payload: dict[str, object],
    failure_type: str,
) -> str:
    """Atomically persist everything needed to replay a failed lineage write."""
    assumptions_relpath = Path("data") / "dcf_assumptions" / f"{ticker}.json"
    identity = json.dumps(
        {
            "ticker": ticker,
            "field_key": field_key,
            "payload": payload,
            "assumptions_path": assumptions_relpath.as_posix(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    receipt_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    receipt_dir = repo_root / "data" / "dcf_assumptions" / "provenance_retry"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    destination = receipt_dir / f"{receipt_id}.json"
    if destination.exists():
        return receipt_id

    receipt = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "status": "retry_pending",
        "retry_operation": "record_driver_provenance",
        "ticker": ticker,
        "field_key": field_key,
        "assumptions_path": assumptions_relpath.as_posix(),
        "payload": payload,
        "failure_type": failure_type,
        "attempts": 0,
        "created_at": datetime.now(UTC).isoformat(),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=receipt_dir,
            prefix=f".{receipt_id}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        assert temporary is not None
        os.replace(temporary, destination)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return receipt_id


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
        raw = cast("dict[str, object]", body).get("inputs")
        if not isinstance(raw, dict):
            return ({"error": "body.inputs (a DCF assumption object) required"}, 400)
        try:
            inp = dcf_redesign.RedesignInputs.from_dict(cast("dict[str, object]", raw))
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
        if result.get("status") != "ok":
            reason = str(result.get("reason", "save failed"))
            code = 409 if "no redesigned workbook" in reason else 500
            return ({"error": reason, "result": result}, code)
        saved_inp = dcf_redesign.read_inputs(repo_root / "dcf" / f"{t}.xlsx")
        response_payload = recompute_payload(saved_inp) if saved_inp is not None else {}
        if saved_inp is not None:
            response_payload["inputs"] = saved_inp.to_dict()
        return {**response_payload, "saved": True, "result": result}

    @blueprint.route("/api/dcf/inject-fact", methods=["POST", "OPTIONS"])
    def dcf_inject_fact():
        if request.method == "OPTIONS":
            return ("", 204)
        from dcf import fact_drivers
        from viewspec.spec import MetricRef, ViewSpecError

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        data = cast("dict[str, object]", body)
        ticker_raw = data.get("ticker")
        token = data.get("token")
        field_key = data.get("field")
        if not isinstance(ticker_raw, str) or not ticker_raw.strip():
            return ({"error": "body.ticker required"}, 400)
        if not isinstance(token, str) or not token.strip():
            return ({"error": "body.token (a picked metric token) required"}, 400)
        if not isinstance(field_key, str) or not field_key.strip():
            return ({"error": "body.field (a driver field key) required"}, 400)
        field = fact_drivers.DRIVER_FIELDS_BY_KEY.get(field_key)
        if field is None:
            return ({"error": f"unknown driver field {field_key!r}"}, 400)
        try:
            metric = MetricRef.parse_token(token)
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)

        try:
            t = ticker_validation.safe_ticker(ticker_raw)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        live = repo_root / "dcf" / f"{t}.xlsx"
        try:
            base_inp = dcf_redesign.read_inputs(live) if live.exists() else None
        except dcf_redesign.RedesignError as exc:
            return ({"error": str(exc)}, 422)
        if base_inp is None:
            return ({"error": f"{t} has no editable FCFF DCF model"}, 404)

        try:
            resolved = fact_drivers.resolve_fact_value(
                metric,
                ticker=t,
                repo_root=repo_root,
                db_path=db_path,
            )
            converted = fact_drivers.convert_to_driver(resolved.value, resolved.unit, field)
            edited = fact_drivers.apply_to_inputs(base_inp, field, converted.value)
        except fact_drivers.FactDriverError as exc:
            return ({"error": str(exc)}, 422)
        try:
            recompute_payload(edited)
        except dcf_redesign.RedesignError as exc:
            return ({"error": str(exc)}, 422)

        import refresh_dcf

        result = refresh_dcf.apply_edits(t, repo_root, db_path, edited)
        if result.get("status") != "ok":
            reason = str(result.get("reason", "injection failed"))
            code = 404 if "no redesigned workbook" in reason else 500
            return ({"error": reason, "result": result}, code)

        injection: dict[str, object] = {
            "ticker": t,
            "field_key": field.key,
            "field_label": field.label,
            "metric_token": token,
            "metric_label": metric.label,
            "raw_value": resolved.value,
            "raw_unit": resolved.unit,
            "applied_value": converted.value,
            "conversion": converted.note,
            "source": resolved.source,
            "fact_id": resolved.fact_id,
            "period_end": resolved.period_end,
        }
        lineage_payload: dict[str, object] = {
            "metric": token,
            "fact_id": resolved.fact_id,
            "raw_value": resolved.value,
            "raw_unit": resolved.unit,
            "applied_value": converted.value,
            "source": resolved.source,
            "period_end": resolved.period_end,
        }
        failure_type = "record_driver_provenance_returned_false"
        try:
            lineage_recorded = fact_drivers.record_driver_provenance(
                repo_root / "data" / "dcf_assumptions" / f"{t}.json",
                field_key=field.key,
                payload=lineage_payload,
            )
        except Exception as exc:
            lineage_recorded = False
            failure_type = type(exc).__name__
        if lineage_recorded:
            provenance: dict[str, object] = {"status": "recorded"}
        else:
            receipt_id = _write_provenance_retry_receipt(
                repo_root,
                ticker=t,
                field_key=field.key,
                payload=lineage_payload,
                failure_type=failure_type,
            )
            provenance = {"status": "retry_pending", "receipt_id": receipt_id}

        saved_inp = dcf_redesign.read_inputs(live)
        response_payload = recompute_payload(saved_inp) if saved_inp is not None else {}
        if saved_inp is not None:
            response_payload["inputs"] = saved_inp.to_dict()
        return {
            **response_payload,
            "injected": True,
            "injection": injection,
            "provenance": provenance,
            "result": result,
        }

    @blueprint.route("/api/dcf/inject-fact-sheet", methods=["POST", "OPTIONS"])
    def dcf_inject_fact_sheet():
        if request.method == "OPTIONS":
            return ("", 204)
        from dcf import fact_drivers, fact_sheet
        from viewspec.spec import MetricRef, ViewSpecError

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        data = cast("dict[str, object]", body)
        ticker_raw = data.get("ticker")
        token = data.get("token")
        if not isinstance(ticker_raw, str) or not ticker_raw.strip():
            return ({"error": "body.ticker required"}, 400)
        if not isinstance(token, str) or not token.strip():
            return ({"error": "body.token (a picked metric token) required"}, 400)
        try:
            metric = MetricRef.parse_token(token)
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)
        try:
            t = ticker_validation.safe_ticker(ticker_raw)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        if not (repo_root / "dcf" / f"{t}.xlsx").exists():
            return ({"error": f"{t} has no DCF model to attach a reference to"}, 404)
        try:
            resolved = fact_drivers.resolve_fact_value(
                metric,
                ticker=t,
                repo_root=repo_root,
                db_path=db_path,
            )
        except fact_drivers.FactDriverError as exc:
            return ({"error": str(exc)}, 422)

        fact = fact_sheet.ReferenceFact(
            token=token,
            label=metric.label,
            value=resolved.value,
            unit=resolved.unit,
            period_end=resolved.period_end,
            source=resolved.source,
            fact_id=resolved.fact_id,
            captured_on=date.today().isoformat(),
        )
        path = fact_sheet.facts_workbook_path(repo_root, t)
        outcome = fact_sheet.upsert_fact(path, fact)
        return {
            "ticker": t,
            "added": True,
            "action": outcome["action"],
            "count": outcome["count"],
            "workbook": str(path),
            "fact": {
                "token": fact.token,
                "label": fact.label,
                "value": fact.value,
                "unit": fact.unit,
                "period_end": fact.period_end,
                "source": fact.source,
                "fact_id": fact.fact_id,
                "captured_on": fact.captured_on,
            },
        }

    @blueprint.route("/api/dcf/reference-facts/<ticker>", methods=["GET"])
    def dcf_reference_facts(ticker: str):
        from dcf import fact_sheet

        t = ticker.upper()
        facts = fact_sheet.read_facts(fact_sheet.facts_workbook_path(repo_root, t))
        return {
            "ticker": t,
            "facts": [
                {
                    "token": fact.token,
                    "label": fact.label,
                    "value": fact.value,
                    "unit": fact.unit,
                    "period_end": fact.period_end,
                    "source": fact.source,
                    "fact_id": fact.fact_id,
                    "captured_on": fact.captured_on,
                }
                for fact in facts
            ],
        }

    return blueprint


def register_dcf_routes(app: Flask, context: DcfRouteContext) -> None:
    """Register the isolated DCF Blueprint on ``app``."""
    app.register_blueprint(create_dcf_blueprint(context))

"""Read-only evidence for calculations over exact report selections."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from html import escape

from flask import Flask, Response, request

from pipeline.peeks import render_canonical_financial_peek
from pipeline.source_viewers import render_record_view
from report.sections.financials import read_growth_evidence
from sources.financial_growth_evidence import GROWTH_FORMULAS, FinancialGrowthReference


def register_growth_evidence_routes(
    app: Flask,
    *,
    get_read_db: Callable[[], sqlite3.Connection],
    safe_ticker: Callable[[str], str],
) -> None:
    @app.route("/api/peek/financial-calculation", methods=["GET"])
    def peek_financial_calculation() -> Response:
        values = request.args.getlist("reference")
        if (
            not set(request.args) <= {"reference", "fragment"}
            or len(values) != 1
            or len(values[0]) > 32768
            or ("fragment" in request.args and request.args.getlist("fragment") != ["1"])
        ):
            return Response("Invalid calculation reference.", status=400, mimetype="text/html")
        try:
            reference = FinancialGrowthReference.model_validate_json(values[0])
            if safe_ticker(reference.inputs[0].ticker) != reference.inputs[0].ticker:
                raise ValueError("invalid ticker")
        except ValueError:
            return Response("Invalid calculation reference.", status=400, mimetype="text/html")
        try:
            result = read_growth_evidence(get_read_db(), reference)
        except (OSError, RuntimeError, sqlite3.Error):
            result = None
        fragment = request.args.get("fragment") == "1"
        if result is None:
            response = Response(
                render_record_view(
                    "Calculation unavailable",
                    "Calculation evidence unavailable for this selection.",
                    fragment=fragment,
                ),
                status=404,
                mimetype="text/html",
            )
        else:
            value, cells = result
            body = (
                '<div class="cc-prov"><div class="cc-prov-row"><b>Calculated growth '
                f'{value:.1%}</b></div><div class="cc-prov-row">'
                f"{escape(GROWTH_FORMULAS[reference.formula])}</div>"
                f'<div class="cc-prov-row">{escape(reference.calculation_version)}</div>'
                '<div class="cc-prov-row">Reported inputs and comparison continuity</div></div>'
            )
            for cell, input_reference in zip(cells, reference.inputs, strict=True):
                body += render_canonical_financial_peek(cell, input_reference)
            response = Response(
                render_record_view(
                    "Evidence for the selected calculation", body, fragment=fragment
                ),
                mimetype="text/html",
            )
        response.headers["Cache-Control"] = "no-store"
        return response

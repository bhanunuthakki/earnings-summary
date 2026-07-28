"""Editable LLM-budget, DCF-global, and ticker-setting routes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from flask import Flask, request


@dataclass(frozen=True)
class SettingsRouteContext:
    db_path: Path


def register_settings_routes(app: Flask, context: SettingsRouteContext) -> None:
    """Register settings APIs directly on ``app``."""
    import llm_budget
    import ticker_settings

    db_path = context.db_path

    @app.route("/api/llm-budgets", methods=["GET"])
    def llm_budgets_api():
        def _num(value: object) -> float:
            return float(str(value))

        budgets = [
            {
                "purpose": str(row["purpose"]),
                "monthly_cap_usd": _num(row["monthly_cap_usd"]),
                "on_exceed": str(row.get("on_exceed", "warn")),
                "current_spend_usd": _num(row["current_spend_usd"]),
                "headroom_pct": _num(row["headroom_pct"]),
                "warn_threshold_pct": _num(row["warn_threshold_pct"]),
                "hard_block": bool(row["hard_block"]),
            }
            for row in llm_budget.list_budgets(db_path=db_path)
        ]
        return {"budgets": budgets}

    @app.route("/api/llm-budgets/<purpose>", methods=["POST", "OPTIONS"])
    def set_llm_budget(purpose: str):
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast(
            "dict[str, object]",
            request.get_json(silent=True) or {},
        )
        cap = body.get("cap_usd")
        mode = body.get("on_exceed")
        if cap is None and mode is None:
            return ({"error": "provide cap_usd and/or on_exceed"}, 400)
        applied = False
        if mode is not None:
            try:
                applied = (
                    llm_budget.set_mode(
                        purpose,
                        str(mode),
                        db_path=db_path,
                    )
                    or applied
                )
            except ValueError as exc:
                return ({"error": str(exc)}, 400)
        if cap is not None:
            if not isinstance(cap, (int, float, str)):
                return (
                    {"error": f"cap_usd must be a number, got {cap!r}"},
                    400,
                )
            try:
                cap_float = float(cap)
            except (TypeError, ValueError):
                return (
                    {"error": f"cap_usd must be a number, got {cap!r}"},
                    400,
                )
            if cap_float < 0:
                return ({"error": "cap_usd must be >= 0"}, 400)
            applied = (
                llm_budget.set_cap(
                    purpose,
                    cap_float,
                    db_path=db_path,
                )
                or applied
            )
        if not applied:
            return ({"error": f"no budget row for purpose {purpose!r}"}, 404)
        return {"purpose": purpose, "ok": True}

    @app.route("/api/dcf-globals", methods=["GET", "POST", "OPTIONS"])
    def dcf_globals_api():
        from dcf import global_assumptions

        if request.method == "OPTIONS":
            return ("", 204)
        if request.method == "GET":
            return {"globals": global_assumptions.get_all(db_path=db_path)}
        body = cast(
            "dict[str, object]",
            request.get_json(silent=True) or {},
        )
        field = body.get("field")
        value = body.get("value")
        if not isinstance(field, str) or value is None:
            return (
                {"error": "provide field (str) and value (number)"},
                400,
            )
        if not isinstance(value, (int, float, str)):
            return (
                {"error": f"value for {field!r} must be a number, got {value!r}"},
                400,
            )
        try:
            numeric_value = float(value)
        except ValueError:
            return (
                {"error": f"value for {field!r} must be a number, got {value!r}"},
                400,
            )
        try:
            ok = global_assumptions.set_value(
                field,
                numeric_value,
                db_path=db_path,
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        if not ok:
            return (
                {"error": "could not persist global DCF assumption (no DB?)"},
                500,
            )
        return {
            "field": field,
            "value": global_assumptions.get(field, db_path=db_path),
            "ok": True,
        }

    @app.route("/api/ticker-settings/<ticker>", methods=["GET", "POST", "OPTIONS"])
    def ticker_settings_api(ticker: str):
        if request.method == "OPTIONS":
            return ("", 204)
        normalized_ticker = ticker.upper()
        if request.method == "GET":
            return {
                "ticker": normalized_ticker,
                "bypass_budget": ticker_settings.get_bypass_budget(
                    normalized_ticker,
                    db_path=db_path,
                ),
            }
        body = cast(
            "dict[str, object]",
            request.get_json(silent=True) or {},
        )
        if "bypass_budget" not in body:
            return ({"error": "bypass_budget required"}, 400)
        value = bool(body["bypass_budget"])
        if not ticker_settings.set_bypass_budget(
            normalized_ticker,
            value,
            db_path=db_path,
        ):
            return (
                {"error": "could not persist (ticker_settings table missing?)"},
                500,
            )
        return {
            "ticker": normalized_ticker,
            "bypass_budget": value,
        }

"""Closed read-only Portfolio Data Service adapter on the private dashboard.

The existing dashboard network/CORS boundary remains authoritative. No target,
path, query, request headers, credentials, or mutation is forwarded from clients.
"""

from flask import Flask, Response, jsonify, request

from integrations.portfolio_tracker_v1 import TrackerV1Client


def register_tracker_read_routes(app: Flask) -> None:
    def read(*, snapshot: bool) -> Response:
        if request.args:
            response = jsonify(error="query_parameters_not_supported")
            response.status_code = 400
        else:
            # Same Windows host as this private dashboard; never accept a
            # client-supplied upstream or resolve a checkout/network fallback.
            client = TrackerV1Client(base_url="http://127.0.0.1:8000")
            fetch = client.get_portfolio_snapshot() if snapshot else client.get_health()
            if not fetch.available or fetch.data is None:
                response = jsonify(error="portfolio_tracker_unavailable")
                response.status_code = 503
            else:
                response = jsonify(fetch.data.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/portfolio-tracker/api/v1/health")
    def tracker_read_health() -> Response:
        return read(snapshot=False)

    @app.get("/portfolio-tracker/api/v1/portfolio-snapshot")
    def tracker_read_snapshot() -> Response:
        return read(snapshot=True)

"""execution/comments_server.py
-------------------------------
Tiny Flask server backing the workspace report's inline-comments + chat
panel. Default port 7421 (matches the `server_url` boot value the renderer
inlines).

Endpoints:
  POST   /comments            create a new comment
  GET    /comments?ticker=&report_date=   list comments for a (ticker, date)
  PATCH  /comments/<id>       update status / append thread
  DELETE /comments/<id>       hard-delete
  POST   /chat/<ticker>       streaming chat (Phase 3 — see workspace_chat.py)
  POST   /chat/<ticker>/apply apply a chatbot-proposed diff (Phase 4)
  GET    /healthz             health check

Usage:
    python execution/comments_server.py
    python execution/comments_server.py --port 7421 --repo-root /path/to/repo

CORS is open for localhost requests (the rendered HTML is opened via
file:// so the browser's origin is `null`). For production / shared use,
tighten the Access-Control-Allow-Origin to a known origin.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

try:
    from flask import Flask, Response, abort, request, send_file, stream_with_context  # noqa: E402
except ImportError:  # pragma: no cover - install hint
    print(
        "Flask not installed. Install with: pip install flask",
        file=sys.stderr,
    )
    sys.exit(1)

import comments  # noqa: E402
import sqlite3  # noqa: E402

from chat_session import build_chat_response, apply_chat_diff  # noqa: E402
from dispatch_registry import Registry, RegistryConflict  # noqa: E402
from pipeline.dashboard_html import render_dashboard_html  # noqa: E402
from pipeline.dashboard_status import build_dashboard_rows  # noqa: E402


def create_app(repo_root: Path, *, registry: Registry | None = None) -> Flask:
    app = Flask(__name__)
    db_path = repo_root / "data" / "portfolio.db"
    job_registry = registry or Registry()
    app.config["DISPATCH_REGISTRY"] = job_registry

    def _open_db() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @app.after_request
    def add_cors_headers(response):
        # file:// renders the HTML so the browser origin is `null`. Allow any
        # origin for localhost-only use; tighten if you bind to 0.0.0.0.
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return {"status": "ok", "repo_root": str(repo_root)}

    # ----- DASHBOARD (PR 1 — read-only) -----

    @app.route("/", methods=["GET"])
    def dashboard_page():
        conn = _open_db()
        try:
            rows = build_dashboard_rows(conn, repo_root)
        finally:
            conn.close()
        return Response(render_dashboard_html(rows), mimetype="text/html")

    @app.route("/api/dashboard", methods=["GET"])
    def dashboard_api():
        conn = _open_db()
        try:
            rows = build_dashboard_rows(conn, repo_root)
        finally:
            conn.close()
        payload = {k: [r.to_dict() for r in v] for k, v in rows.items()}
        return payload

    @app.route("/reports/<ticker>", methods=["GET"])
    def latest_report_for_ticker(ticker: str):
        """Serve the most recently built workspace HTML for the ticker.

        Uses the latest filename (`<DATE>_workspace.html`) since YYYY-MM-DD
        sorts chronologically. Returns 404 if no build exists.
        """
        research_dir = repo_root / "output" / "research" / ticker.upper()
        if not research_dir.exists():
            abort(404)
        matches = sorted(research_dir.glob("*_workspace.html"))
        if not matches:
            abort(404)
        return send_file(matches[-1])

    # ----- ACTIONS (PR 2a — refresh dispatcher) -----

    @app.route("/actions/refresh", methods=["POST", "OPTIONS"])
    def start_refresh():
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        try:
            ticker = str(body["ticker"]).upper()
            mode = body.get("mode", "stale")
        except (KeyError, TypeError):
            return ({"error": "ticker required"}, 400)
        if mode not in ("stale", "full"):
            return ({"error": f"mode must be 'stale' or 'full', got {mode!r}"}, 400)

        dispatcher = repo_root / "execution" / "refresh_dispatch.py"
        argv = [sys.executable, str(dispatcher), "--ticker", ticker, "--mode", mode]
        try:
            job = job_registry.start(
                ticker=ticker, kind=f"refresh-{mode}", argv=argv,
            )
        except RegistryConflict as e:
            return ({"error": str(e)}, 409)

        return ({
            "job_id": job.job_id,
            "ticker": job.ticker,
            "kind": job.kind,
            "stream_url": f"/actions/stream/{job.job_id}",
            "started_at": job.started_at.isoformat(),
        }, 201)

    @app.route("/actions/stream/<job_id>", methods=["GET"])
    def stream_action(job_id: str):
        job = job_registry.get(job_id)
        if job is None:
            return ({"error": "job not found"}, 404)
        return Response(
            stream_with_context(job.stream_events()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/actions/jobs", methods=["GET"])
    def list_jobs():
        return {"jobs": job_registry.list_jobs()}

    # ----- COMMENTS -----

    @app.route("/comments", methods=["OPTIONS"])
    def comments_options():
        return ("", 204)

    @app.route("/comments", methods=["GET"])
    def list_comments_endpoint():
        ticker = request.args.get("ticker")
        report_date_str = request.args.get("report_date")
        if not ticker or not report_date_str:
            return ({"error": "ticker and report_date required"}, 400)
        report_date = _parse_date(report_date_str)
        store = comments.load_store(repo_root, ticker, report_date)
        return Response(store.model_dump_json(indent=2), mimetype="application/json")

    @app.route("/comments", methods=["POST"])
    def create_comment_endpoint():
        body = request.get_json(silent=True) or {}
        try:
            ticker = body["ticker"]
            report_date = _parse_date(body["report_date"])
            anchor = comments.Anchor(**body["anchor"])
            text = body["comment"]
            intent = body.get("intent") or None
            selected_text = body.get("selected_text")
        except (KeyError, ValueError, TypeError) as e:
            return ({"error": f"bad payload: {e}"}, 400)
        c = comments.append_comment(
            repo_root, ticker, report_date,
            anchor=anchor, text=text, selected_text=selected_text, intent=intent,
        )
        return Response(c.model_dump_json(), mimetype="application/json", status=201)

    @app.route("/comments/<comment_id>", methods=["PATCH", "OPTIONS"])
    def patch_comment_endpoint(comment_id: str):
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        try:
            ticker = body["ticker"]
            report_date = _parse_date(body["report_date"])
        except (KeyError, ValueError, TypeError):
            return ({"error": "ticker + report_date required"}, 400)
        status = body.get("status")
        resolution = body.get("resolution_note")
        intent = body.get("intent")
        updated = comments.update_comment(
            repo_root, ticker, report_date, comment_id,
            status=status, resolution_note=resolution, intent=intent,
        )
        if updated is None:
            return ({"error": "comment not found"}, 404)
        return Response(updated.model_dump_json(), mimetype="application/json")

    @app.route("/comments/<comment_id>", methods=["DELETE"])
    def delete_comment_endpoint(comment_id: str):
        body = request.get_json(silent=True) or {}
        try:
            ticker = body["ticker"]
            report_date = _parse_date(body["report_date"])
        except (KeyError, ValueError, TypeError):
            return ({"error": "ticker + report_date required"}, 400)
        ok = comments.delete_comment(repo_root, ticker, report_date, comment_id)
        return ({"deleted": ok}, 200 if ok else 404)

    # ----- CHAT (Phase 3) -----

    @app.route("/chat/<ticker>", methods=["OPTIONS"])
    def chat_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>", methods=["GET"])
    def list_chat_endpoint(ticker: str):
        report_date_str = request.args.get("report_date")
        if not report_date_str:
            return ({"error": "report_date required"}, 400)
        report_date = _parse_date(report_date_str)
        thread = build_chat_response.load_thread(repo_root, ticker, report_date)
        return ({"ticker": ticker, "report_date": report_date.isoformat(),
                 "thread": [t.model_dump(mode="json") for t in thread]}, 200)

    @app.route("/chat/<ticker>", methods=["POST"])
    def chat_endpoint(ticker: str):
        body = request.get_json(silent=True) or {}
        try:
            report_date = _parse_date(body["report_date"])
            user_message = body["message"]
        except (KeyError, ValueError, TypeError) as e:
            return ({"error": f"bad payload: {e}"}, 400)

        def generate():
            for chunk in build_chat_response.stream_response(
                repo_root=repo_root,
                ticker=ticker,
                report_date=report_date,
                user_message=user_message,
            ):
                # Server-Sent Events frame
                yield f"data: {json.dumps(chunk)}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ----- APPLY (Phase 4) -----

    @app.route("/chat/<ticker>/apply", methods=["OPTIONS"])
    def chat_apply_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>/apply", methods=["POST"])
    def chat_apply_endpoint(ticker: str):
        body = request.get_json(silent=True) or {}
        try:
            diff = body["diff"]
            dry_run = bool(body.get("dry_run", False))
        except (KeyError, TypeError):
            return ({"error": "diff required"}, 400)
        result = apply_chat_diff(repo_root, ticker, diff, dry_run=dry_run)
        return result

    return app


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7421)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    print(
        f"comments_server: repo_root={repo_root} host={args.host} port={args.port}",
        file=sys.stderr,
    )
    app = create_app(repo_root)
    # Flask's built-in dev server is fine here — this is a single-user
    # localhost tool, not a production service.
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

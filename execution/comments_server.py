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

CORS is wide-open (`Access-Control-Allow-Origin: *`) only when the request
Host is localhost — the workspace HTML opens via file:// so the browser
origin is `null`, which would be rejected by any non-`*` policy. If you
bind to 0.0.0.0 or another interface, set `COMMENTS_SERVER_CORS_WHITELIST`
to a comma-separated list of allowed Origins; the server echoes the
request's Origin back only when it matches.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import queue
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


def create_app(
    repo_root: Path,
    *,
    registry: Registry | None = None,
    chat_executor: concurrent.futures.Executor | None = None,
) -> Flask:
    app = Flask(__name__)
    db_path = repo_root / "data" / "portfolio.db"
    job_registry = registry or Registry()
    app.config["DISPATCH_REGISTRY"] = job_registry
    # Dedicated pool so a long-running LLM subprocess doesn't pin a Flask
    # request thread for the full 10-60s of a chat turn. Pool size caps
    # the number of concurrent chats; chunks flow back via per-request
    # queues. Tests can inject their own executor for isolation.
    chat_pool = chat_executor or concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="comments-server-chat"
    )
    app.config["CHAT_EXECUTOR"] = chat_pool

    def _open_db() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @app.after_request
    def add_cors_headers(response):
        # file:// renders the HTML so the browser origin is `null`. `*` is
        # the only Allow-Origin value that satisfies a null origin, so we
        # emit it for localhost only — if someone runs --host 0.0.0.0 a
        # wide-open header becomes a footgun. For non-localhost hosts,
        # echo back the Origin only when it's in the whitelist env var.
        host = (request.host or "").split(":", 1)[0]
        if host in ("127.0.0.1", "localhost", "[::1]"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        else:
            whitelist = [
                o.strip()
                for o in os.environ.get("COMMENTS_SERVER_CORS_WHITELIST", "").split(",")
                if o.strip()
            ]
            origin = request.headers.get("Origin", "")
            if origin and origin in whitelist:
                response.headers["Access-Control-Allow-Origin"] = origin
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

    @app.route("/actions/refresh-ir", methods=["POST", "OPTIONS"])
    def start_refresh_ir():
        """Refresh a ticker's KPIs from its IR historical-data spreadsheet.

        Runs execution/refresh_ir_kpis.py --discover (headless browser resolves
        the current spreadsheet URL → download → parse → tier-ingest, superseding
        the LLM brief/press values). Streams via /actions/stream/<job_id> like
        /actions/refresh.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        ticker = str(body.get("ticker", "")).upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        try:
            quarters = int(body.get("quarters", 8))
        except (TypeError, ValueError):
            return ({"error": "quarters must be an integer"}, 400)

        script = repo_root / "execution" / "refresh_ir_kpis.py"
        argv = [
            sys.executable, str(script), "--ticker", ticker,
            "--discover", "--quarters", str(quarters),
            "--repo-root", str(repo_root),
        ]
        try:
            job = job_registry.start(ticker=ticker, kind="refresh-ir", argv=argv)
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

        # The LLM subprocess (Claude CLI) drives `stream_response` for
        # 10-60s; running it inline would pin the Flask request thread
        # for that whole window. Dispatch to the chat pool and pipe its
        # chunks through a Queue, then drain the queue into SSE frames.
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue()
        chat_pool.submit(
            _drain_chat_stream,
            repo_root, ticker, report_date, user_message, chunks,
        )

        def generate():
            while True:
                item = chunks.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"

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


def _drain_chat_stream(
    repo_root: Path,
    ticker: str,
    report_date: date,
    user_message: str,
    chunks: queue.Queue[dict[str, object] | None],
) -> None:
    """Iterate `stream_response` on a pool thread and push each chunk
    onto `chunks`. `None` marks end-of-stream so the SSE generator on
    the request thread can stop pumping."""
    try:
        for chunk in build_chat_response.stream_response(
            repo_root=repo_root,
            ticker=ticker,
            report_date=report_date,
            user_message=user_message,
        ):
            chunks.put(chunk)
    except Exception as e:
        # Surface as an SSE error frame instead of crashing the pool —
        # the request thread is still waiting on the queue and needs the
        # `None` sentinel below to terminate.
        chunks.put({"type": "error", "error": f"chat stream failed: {e}"})
    finally:
        chunks.put(None)


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

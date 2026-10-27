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
  GET/POST /chat/<ticker>     retired report-chat compatibility tombstone
  POST   /chat/<ticker>/apply retired proposal compatibility tombstone
  POST   /api/ask/stream      the single research-conversation path (SSE
                              session/stage/delta/fragment/final frames)
  GET    /healthz             health check

Usage:
    python execution/sqlite_bootstrap.py execution/comments_server.py
    python execution/sqlite_bootstrap.py execution/comments_server.py --port 7421 --repo-root /path/to/repo

CORS: the server never emits `Access-Control-Allow-Origin: *`. It echoes back
only the file:// renderer's `null` Origin and loopback Origins — so the local
dashboard works while a cross-site Origin gets no CORS header (CSRF defense).
If you bind to 0.0.0.0 or another interface, set `COMMENTS_SERVER_CORS_WHITELIST`
to a comma-separated list of allowed Origins; the server echoes a request's
Origin back only when it matches.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from flask import Flask
except ImportError:  # pragma: no cover - install hint
    print(
        "Flask not installed. Install with: pip install flask",
        file=sys.stderr,
    )
    sys.exit(1)

from comments_server_tracker_routes import register_tracker_read_routes

from db_paths import require_db_path
from dcf.input_evidence import SourceReadContext
from dispatch_registry import Registry
from logging_config import (
    configure_logging,
)
from operations.models import OperationsRegistry
from runtime.secrets import load_project_env
from server_runtime.access import (
    resolve_tailscale_ipv4,
    validate_bind_host,
)

if TYPE_CHECKING:
    pass

from comments_server_actions_routes import register_actions_routes
from comments_server_advisor_routes import register_advisor_routes
from comments_server_chat_routes import register_chat_routes
from comments_server_comments_routes import register_comments_routes
from comments_server_context import build_server_context
from comments_server_core_routes import register_core_routes
from comments_server_ops_routes import register_ops_routes
from comments_server_panel_metrics_routes import register_panel_metrics_routes
from comments_server_panel_routes import register_panel_routes
from comments_server_shared import (
    LOGGER,
    PROJECT_ROOT,
    RedactingFlask,
    drain_durable_events,
)
from comments_server_sibling_wiring import register_sibling_routes
from comments_server_views_discovery_routes import register_views_discovery_routes
from comments_server_viewspec_ask_routes import register_viewspec_ask_routes
from comments_server_work_os_routes import register_work_os_routes

# The disconnect-safe drain was historically importable from this module.
_drain_durable_events = drain_durable_events


def create_app(
    repo_root: Path,
    *,
    db_path: Path | None = None,
    registry: Registry | None = None,
    operations_registry: OperationsRegistry | None = None,
    code_root: Path | None = None,
    chat_executor: concurrent.futures.Executor | None = None,
    server_origin: str | None = None,
    source_context: SourceReadContext | None = None,
    source_state_root: Path | None = None,
) -> Flask:
    """Wire the cockpit: build the shared context, then register every route family.

    The route bodies, the panel registry, the request middleware, and the
    module-level helpers all live in their own ``comments_server_*`` modules;
    this factory is the composition root.
    """
    if source_state_root is not None:
        child_context = SourceReadContext.for_product_state_root(source_state_root)
        if source_context is None or source_context == SourceReadContext.for_sec_state_root(
            source_state_root
        ):
            source_context = child_context
        elif source_context != child_context:
            raise ValueError("source reader and child state root authorities differ")
    app = RedactingFlask(__name__)
    register_tracker_read_routes(app)
    ctx = build_server_context(
        app,
        repo_root,
        db_path=db_path,
        registry=registry,
        operations_registry=operations_registry,
        code_root=code_root,
        chat_executor=chat_executor,
        server_origin=server_origin,
        source_context=source_context,
        source_state_root=source_state_root,
    )
    register_core_routes(app, ctx)
    register_work_os_routes(app, ctx)
    register_ops_routes(app, ctx)
    register_panel_routes(app, ctx)
    register_panel_metrics_routes(app, ctx)
    register_viewspec_ask_routes(app, ctx)
    register_views_discovery_routes(app, ctx)
    register_actions_routes(app, ctx)
    register_advisor_routes(app, ctx)
    register_comments_routes(app, ctx)
    register_chat_routes(app, ctx)
    register_sibling_routes(app, ctx)
    return app


def configure_runtime_db(repo_root: Path) -> Path:
    """Bind implicit DB consumers to the same canonical DB as request handlers."""
    import db

    load_project_env(repo_root)
    configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    if not configured:
        raise RuntimeError("An explicit configured portfolio database is required")
    db_path = require_db_path(configured)
    db.set_db_path(db_path, state_root=repo_root)
    return db_path


def main() -> int:
    configure_logging()  # structured root logging + correlation ids (sre-4)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7421)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--host")
    parser.add_argument(
        "--tailscale",
        action="store_true",
        help="Bind to this device's Tailscale IPv4 address; no app login is added.",
    )
    args = parser.parse_args()

    repo_root = (args.repo_root or PROJECT_ROOT).resolve()
    source_state_root = args.state_root
    if source_state_root is None and args.repo_root is not None:
        source_state_root = repo_root
    db_path = configure_runtime_db(repo_root)
    if args.tailscale:
        os.environ["COMMENTS_SERVER_ALLOW_TAILSCALE"] = "1"
    host = args.host or (resolve_tailscale_ipv4() if args.tailscale else "127.0.0.1")
    try:
        validate_bind_host(host, allow_tailscale=args.tailscale)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        f"comments_server: repo_root={repo_root} host={host} port={args.port}",
        file=sys.stderr,
    )
    origin_host = f"[{host}]" if ":" in host else host
    app = create_app(
        repo_root,
        db_path=db_path,
        server_origin=f"http://{origin_host}:{args.port}",
        source_state_root=source_state_root,
        source_context=SourceReadContext.for_product_state_root(source_state_root)
        if source_state_root is not None
        else None,
    )
    # Each Ask request checks its own sealed evidence coordinates. Avoid a
    # second registry/database traversal on the server's startup path.
    LOGGER.info("ask_retrieval_readiness deferred_to_request=true fail_closed=true")
    # Flask's built-in dev server is fine here — this is a single-user
    # localhost tool, not a production service.
    app.run(host=host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

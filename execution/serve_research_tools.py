"""Serve the read-only research tools to an orchestrator agent over MCP stdio.

The client (Claude Code, Codex or another MCP host) starts this process and
talks newline-delimited JSON-RPC on stdin/stdout. Structured events go to
stderr. The database must be an explicit authorized path; the server opens it
read-only and never calls a model.

Usage:
    python execution/serve_research_tools.py --db-path <explicit portfolio.db>
"""

from __future__ import annotations

import sys

try:  # direct script invocation
    from _lib import PROJECT_ROOT, add_database_argument, command_parser, require_db_path
except ImportError:  # pragma: no cover - package import fallback
    from execution._lib import PROJECT_ROOT, add_database_argument, command_parser, require_db_path

from ask.tool_server import ResearchToolSession, serve


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    add_database_argument(parser)
    args = parser.parse_args(argv)
    session = ResearchToolSession(db_path=require_db_path(args.db_path), repo_root=PROJECT_ROOT)
    serve(session, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

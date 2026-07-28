from __future__ import annotations

from pathlib import Path


def test_server_never_accepts_request_controlled_user_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    execution = root / "execution"
    route_sources = [
        execution / "comments_server.py",
        *sorted(execution.glob("comments_server*_routes.py")),
    ]
    for path in route_sources:
        source = path.read_text(encoding="utf-8")
        assert 'request.args.get("user_id"' not in source, path
        assert 'payload.get("user_id")' not in source, path
        assert 'body.get("user_id")' not in source, path

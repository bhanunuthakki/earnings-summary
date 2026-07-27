from __future__ import annotations

from pathlib import Path


def test_server_never_accepts_request_controlled_user_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "execution" / "comments_server.py").read_text(encoding="utf-8")
    assert 'request.args.get("user_id"' not in source
    assert 'payload.get("user_id")' not in source
    assert 'body.get("user_id")' not in source

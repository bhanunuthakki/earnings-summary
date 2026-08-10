"""The retired report-chat JSON session path must not remain callable."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_json_chat_session_surface_is_physically_removed() -> None:
    assert not (PROJECT_ROOT / "src" / "chat_session.py").exists()

    production_sources = [
        *sorted((PROJECT_ROOT / "src").rglob("*.py")),
        *sorted((PROJECT_ROOT / "execution").rglob("*.py")),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in production_sources)

    assert "import chat_session" not in combined
    assert "from chat_session" not in combined
    assert "build_ticker_pack" not in combined
    assert "data/report_chats" not in combined

    ask_surface = "\n".join(
        (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for relative in ("src/ask/context.py", "src/ask/engine.py")
    )
    assert 'scope == "ticker"' not in ask_surface

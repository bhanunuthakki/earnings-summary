"""The Ledger capture poller — poll_once orchestration over a mocked Telegram."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import poller, telegram, transcribe
from capture.matcher import build_roster_index
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU", "MELI"], phrases={"nubank": "NU"})


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def test_load_save_offset_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "offset.json"
    assert poller.load_offset(p) is None
    poller.save_offset(p, 42)
    assert poller.load_offset(p) == 42


def test_poll_once_ingests_text_and_advances_offset(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [
        telegram.Update(update_id=10, kind="text", chat_id=1, text="Nubank looks compelling"),
        telegram.Update(update_id=11, kind="text", chat_id=1, text="the market feels toppy"),
    ]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    offset_path = tmp_path / "offset.json"
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=offset_path,
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=False,
    )
    assert counts["updates"] == 2
    assert counts.get("landed") == 2
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 2
    assert json.loads(offset_path.read_text(encoding="utf-8"))["offset"] == 12


def test_poll_once_dedups_on_replay(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [telegram.Update(update_id=20, kind="text", chat_id=1, text="MELI looks cheap")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    offset_path = tmp_path / "offset.json"
    audio = tmp_path / "audio"
    poller.poll_once(
        "t", db_path=db_path, offset_path=offset_path, audio_dir=audio, roster=ROSTER, confirm=False
    )
    counts = poller.poll_once(
        "t", db_path=db_path, offset_path=offset_path, audio_dir=audio, roster=ROSTER, confirm=False
    )
    assert counts.get("duplicate") == 1
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_poll_once_voice_downloads_lands_and_purges_audio(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [telegram.Update(update_id=30, kind="voice", chat_id=1, voice_file_id="F1")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    monkeypatch.setattr(telegram, "get_file_path", lambda token, file_id: "voice/f1.oga")

    def _download(token: str, file_path: str, dest: object) -> Path:
        out = Path(str(dest))
        out.write_bytes(b"\x00")
        return out

    monkeypatch.setattr(telegram, "download_file", _download)
    monkeypatch.setattr(transcribe, "transcribe", lambda path: "thinking about Nubank credit")
    audio = tmp_path / "audio"
    counts = poller.poll_once(
        "t",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=audio,
        roster=ROSTER,
        confirm=False,
    )
    assert counts.get("landed") == 1
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 1
    assert musings[0].ticker == "NU"
    assert not (audio / "tg_30.oga").exists()  # audio purged once landed


def test_poll_once_skips_bot_commands(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [
        telegram.Update(update_id=40, kind="text", chat_id=1, text="/start"),
        telegram.Update(update_id=41, kind="text", chat_id=1, text="Nubank looks good here"),
    ]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command") == 1
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 1  # only the real thought landed, not /start
    assert "Nubank looks good" in musings[0].body
    assert any("capture is live" in s for s in sent)  # /start got a greeting

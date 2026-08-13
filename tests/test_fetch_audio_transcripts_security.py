# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""Security and resource-bound regressions for audio transcript fetching.

The suite intentionally probes private URL/resource guards and replaces the
third-party downloader with dynamic test doubles.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))

import fetch_audio_transcripts as audio


def test_audio_pipeline_is_denied_before_downloader_or_transcriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        audio,
        "_select_audio_url",
        lambda *_args, **_kwargs: pytest.fail("network boundary was crossed"),
    )

    with pytest.raises(audio.AudioCollectionPolicyError, match="excluded"):
        audio.fetch_and_transcribe(audio.FetchSpec(ticker="ACME", year=2026, quarter=2), None)


def test_validate_audio_url_rejects_private_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(_url: str) -> str:
        raise audio.UnsafeURLError("non-public host blocked: http://127.0.0.1/private")

    monkeypatch.setattr(audio, "ensure_safe_public_url", reject)

    with pytest.raises(audio.AudioFetchError) as caught:
        audio._validate_audio_url("http://127.0.0.1/private?access_token=SECRET")

    rendered = str(caught.value)
    assert "127.0.0.1" not in rendered
    assert "SECRET" not in rendered
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=123",
        urlunsplit(("https", "user:password@youtube.com", "/watch", "v=123", "")),
    ],
)
def test_validate_audio_url_rejects_unapproved_or_credentialed_url(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    monkeypatch.setattr(audio, "ensure_safe_public_url", lambda value: value)

    with pytest.raises(audio.AudioFetchError):
        audio._validate_audio_url(url)


def test_validate_audio_url_accepts_public_youtube_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio, "ensure_safe_public_url", lambda value: value)

    url = "https://www.youtube.com/watch?v=123"
    assert audio._validate_audio_url(url) == url


def test_download_uses_bounded_options_and_sanitizes_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeYdl:
        def __init__(self, options: dict[str, Any]) -> None:
            captured.update(options)

        def __enter__(self) -> FakeYdl:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def download(self, _urls: list[str]) -> None:
            raise RuntimeError("failed https://www.youtube.com/watch?v=123&access_token=TOPSECRET")

    monkeypatch.setattr(audio, "_validate_audio_url", lambda value: value)
    monkeypatch.setattr(audio.yt_dlp, "YoutubeDL", FakeYdl)

    with pytest.raises(audio.AudioCollectionPolicyError, match="excluded") as caught:
        audio._download_audio(
            "https://www.youtube.com/watch?v=123&access_token=TOPSECRET",
            tmp_path / "audio",
            None,
        )

    assert captured == {}
    rendered = str(caught.value)
    assert "youtube.com" not in rendered
    assert "TOPSECRET" not in rendered
    assert caught.value.__cause__ is None


def test_download_progress_hook_enforces_byte_cap() -> None:
    with pytest.raises(audio.AudioFetchError):
        audio._enforce_download_bound({"downloaded_bytes": audio.MAX_AUDIO_BYTES + 1})

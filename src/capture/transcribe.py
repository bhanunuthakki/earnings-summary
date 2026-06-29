"""Local voice→text via faster-whisper — no sidecar, no cloud, $0.

``faster_whisper`` is already a project dependency (execution/fetch_audio_
transcripts.py) wired to the off-PATH ffmpeg at ``C:/ffmpeg/bin``. "Local STT"
here is a library call, not a process to babysit: the model is loaded ONCE
(module global) and reused for every musing. The import is lazy so the capture
package — and text-only paths / CI — never need faster-whisper unless a voice
memo actually arrives.

Accuracy of proper nouns (ticker names) is not load-bearing here: the ticker is
matched DETERMINISTICALLY over the roster (matcher.py), so a misheard name yields
``needs_ticker``, never a wrong attribution. On any failure the caller RETAINS the
audio for retry — a musing's words are never lost to a transcription error.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, cast

# Default model: English-only ``base.en`` (fast, low memory). Upgrade to
# ``small.en`` via CAPTURE_WHISPER_MODEL if word-error on a golden audio set is
# poor; clips are short (seconds to minutes) so latency is not a concern.
_DEFAULT_MODEL = "base.en"
_WINDOWS_FFMPEG = Path("C:/ffmpeg/bin")

_MODEL: object | None = None  # loaded once, reused for every transcription


class TranscriptionError(RuntimeError):
    """Voice could not be transcribed (missing audio, decode error, empty, or
    faster-whisper unavailable). The caller keeps the raw audio for retry."""


class _Segment(Protocol):
    text: str


class _WhisperLike(Protocol):
    def transcribe(self, audio: str, *, beam_size: int) -> tuple[Iterable[_Segment], object]: ...


def _model_name() -> str:
    return os.environ.get("CAPTURE_WHISPER_MODEL", _DEFAULT_MODEL)


def _ensure_ffmpeg_on_path() -> None:
    """faster-whisper decodes via bundled libs, but on Windows prepend the known
    ffmpeg dir to PATH defensively for any codec PyAV shells out for."""
    if os.name == "nt" and _WINDOWS_FFMPEG.exists():
        ffmpeg = str(_WINDOWS_FFMPEG)
        if ffmpeg not in os.environ.get("PATH", ""):
            os.environ["PATH"] = ffmpeg + os.pathsep + os.environ.get("PATH", "")


def _load_model() -> object:
    """Construct the WhisperModel (lazy import; raises TranscriptionError when
    faster-whisper is unavailable). Separated so tests can stub it."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise TranscriptionError("faster-whisper is not installed") from exc
    _ensure_ffmpeg_on_path()
    return WhisperModel(_model_name(), device="cpu", compute_type="int8")


def _get_model() -> object:
    global _MODEL
    if _MODEL is None:
        _MODEL = _load_model()
    return _MODEL


def transcribe(audio_path: Path | str) -> str:
    """Transcribe a voice memo (.oga/.ogg/.wav/.m4a…) to plain prose.

    Raises ``TranscriptionError`` on any failure (missing file, decode error,
    empty result) — the caller retains the audio and retries rather than losing
    the musing."""
    path = Path(audio_path)
    if not path.exists():
        raise TranscriptionError(f"audio not found: {path}")
    try:
        model = cast("_WhisperLike", _get_model())
        segments, _info = model.transcribe(str(path), beam_size=1)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except TranscriptionError:
        raise
    except Exception as exc:  # decode / model runtime failure
        raise TranscriptionError(f"transcription failed for {path.name}: {exc}") from exc
    if not text:
        raise TranscriptionError(f"empty transcription for {path.name}")
    return text

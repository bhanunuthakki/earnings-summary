"""
execution/fetch_audio_transcripts.py
------------------------------------
Layer 3 execution script: fetch earnings call audio from YouTube and transcribe locally.

Three input modes (mutually exclusive):
  1. --url          Explicit YouTube URL for a single (ticker, year, quarter).
  2. --links-file   JSON manifest of curated URLs (see directives/fetch_transcripts.md).
  3. (default)      Smart search fallback: yt-dlp ytsearchN with title scoring +
                    duration filtering. Used when no URL is curated.

The smart search replaces the previous naive `ytsearch1` which often grabbed
analyst recap videos instead of the actual call.

Output:
  - transcripts/raw/{TICKER}_Q{N}_{YEAR}.txt   (timestamped Whisper text)
  - .tmp/transcript_index.json entry           (source label disambiguates origin)

ffmpeg is required by yt-dlp (audio extract) and faster-whisper (decode). On
Windows where ffmpeg isn't on PATH, set FFMPEG_LOCATION env var (or pass
--ffmpeg-location) to the directory containing ffmpeg.exe.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yt_dlp
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.append(str(SRC_DIR))

import index_manager  # noqa: E402
from alias_manager import resolve_ticker  # noqa: E402
from compute.transcript_ingest import (  # noqa: E402
    QASectionStatus,
    detect_qa_section,
    qa_status_to_db_value,
)
from ir_pipeline._net import UnsafeURLError, ensure_safe_public_url  # noqa: E402
from log_redact import redact  # noqa: E402
from models.documents import DocType, SourceType  # noqa: E402
from pipeline.transcript_acquisition import (  # noqa: E402
    COMBINED_SOURCE_REGIME_IDENTITY,
    authorize_transcript_request,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from transcript_qa import (  # noqa: E402
    QaStatus,
    validate_audio_transcript,
    validate_transcript,
)
from transcripts.acquisition_semantics import (  # noqa: E402
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    TranscriptAcquisitionAuthorization,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptAuthorizationFailure,
    TranscriptAuthorizationStatus,
    TranscriptProvider,
)

RAW_DIR = PROJECT_ROOT / "transcripts" / "raw"
TMP_DIR = PROJECT_ROOT / ".tmp"

# Earnings calls: typical 30-110 min; reject anything outside this band.
MIN_DURATION_SEC = 25 * 60
MAX_DURATION_SEC = 130 * 60

# Title-scoring vocabulary. Keep small and explicit — no fuzzy matching.
POSITIVE_TITLE_TOKENS = (
    "earnings conference call",
    "earnings call",
    "earnings webcast",
)
NEGATIVE_TITLE_TOKENS = (
    "analysis",
    "recap",
    "highlights",
    "breakdown",
    "review",
    "reaction",
    "summary",
    "explained",
    "predicts",
    "preview",
    "vs ",
    "should i buy",
    "stock analysis",
    "deep dive",
)

WINDOWS_FFMPEG_DEFAULT = Path("C:/ffmpeg/bin")

# Whisper model + decode defaults. distil-large-v3 + greedy decode is ~6-8x
# faster than large-v3-turbo + beam_size=5 on CPU/int8 with ~99% relative WER.
# Override via --whisper-model / --beam-size / WHISPER_MODEL / WHISPER_BEAM_SIZE.
DEFAULT_WHISPER_MODEL = "distil-large-v3"
DEFAULT_BEAM_SIZE = 1

# Bound yt-dlp even for a curated URL. The duration gate catches search
# candidates, while these limits also cover explicit URLs and incomplete
# upstream metadata.
YDL_SOCKET_TIMEOUT_SEC = 30
YDL_RETRIES = 3
MAX_AUDIO_BYTES = 512 * 1024 * 1024

_ALLOWED_AUDIO_HOSTS = frozenset(
    {
        "youtu.be",
        "youtube.com",
        "youtube-nocookie.com",
    }
)
_URL_IN_MESSAGE_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TranscriptSource(str, Enum):  # noqa: UP042 - preserve legacy str(Enum) behavior
    YT_DLP_WHISPER_URL = "yt_dlp_whisper_url"  # explicit URL provided
    YT_DLP_WHISPER_SEARCH = "yt_dlp_whisper_search"  # picked via smart search
    YT_DLP_WHISPER_LINKS = "yt_dlp_whisper_links"  # picked from links file


class FetchSpec(BaseModel):
    """One quarter's fetch input — validated before any network/disk work."""

    ticker: str
    year: int = Field(ge=2000, le=2100)
    quarter: int = Field(ge=1, le=4)
    url: HttpUrl | None = None

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class LinksManifestEntry(BaseModel):
    ticker: str
    year: int = Field(ge=2000, le=2100)
    quarter: int = Field(ge=1, le=4)
    url: HttpUrl | None = None
    title: str | None = None
    gap_reason: str | None = None  # set when url is null and the gap is intentional

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class LinksManifest(BaseModel):
    links: list[LinksManifestEntry]


@dataclass(frozen=True)
class TranscriptionResult:
    ticker: str
    year: int
    quarter: int
    output_path: Path
    source: TranscriptSource
    video_url: str
    qa_section_status: QASectionStatus
    qa_section_signals: tuple[str, ...]


class AudioFetchError(RuntimeError):
    """A media URL was unsafe or yt-dlp could not fetch it within the bounds."""


class AudioCollectionPolicyError(AudioFetchError):
    """Audio/webcast collection is disabled by the canonical source policy."""


def _select_audio_url(
    spec: FetchSpec,
    ffmpeg_location: Path | None,
) -> str:
    """Legacy selector retained behind the policy gate for explicit testability."""

    if spec.url is not None:
        return _validate_audio_url(str(spec.url))
    return smart_search_url(spec.ticker, spec.year, spec.quarter, ffmpeg_location)


def _enforce_audio_policy(
    authorization: TranscriptAcquisitionAuthorization | None = None,
) -> None:
    """Fail closed before every downloader/search boundary; webcasts are excluded."""

    if (
        authorization is not None
        and authorization.status is TranscriptAuthorizationStatus.DENIED
        and authorization.failure is TranscriptAuthorizationFailure.AUDIO_WEBCAST_EXCLUDED
        and authorization.request.entrypoint
        is TranscriptAcquisitionEntrypoint.FETCH_AUDIO_TRANSCRIPTS
        and authorization.request.provider is TranscriptProvider.YOUTUBE_AUDIO
    ):
        raise AudioCollectionPolicyError(
            "Audio/webcast collection is excluded by canonical transcript policy "
            f"({authorization.idempotency_key})."
        )
    raise AudioCollectionPolicyError(
        "Audio/webcast collection is excluded; the canonical denied receipt is required."
    )


def _safe_external_message(value: object) -> str:
    """Redact credentials and remove full URLs from third-party diagnostics."""
    return _URL_IN_MESSAGE_RE.sub("[redacted-url]", redact(value))


class _RedactingYdlLogger:
    """Keep yt-dlp diagnostics useful without exposing curated URLs or tokens."""

    @staticmethod
    def debug(message: str) -> None:
        if message.startswith("[debug] "):
            return
        sys.stderr.write(f"{_safe_external_message(message)}\n")

    @staticmethod
    def info(message: str) -> None:
        sys.stderr.write(f"{_safe_external_message(message)}\n")

    @staticmethod
    def warning(message: str) -> None:
        sys.stderr.write(f"WARN {_safe_external_message(message)}\n")

    @staticmethod
    def error(message: str) -> None:
        sys.stderr.write(f"ERROR {_safe_external_message(message)}\n")


def _validate_audio_url(url: str) -> str:
    """Require a credential-free, public YouTube URL before yt-dlp sees it."""
    try:
        safe_url = ensure_safe_public_url(url)
    except (UnsafeURLError, ValueError):
        raise AudioFetchError("Refusing unsafe or non-public audio URL.") from None

    parsed = urlparse(safe_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.username is not None or parsed.password is not None:
        raise AudioFetchError("Refusing an audio URL containing user credentials.")
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _ALLOWED_AUDIO_HOSTS):
        raise AudioFetchError("Refusing a non-YouTube audio URL.")
    return safe_url


def _safe_url_label(url: str) -> str:
    """Return only a hostname for progress output; never echo a path or query."""
    return (urlparse(url).hostname or "approved media host").lower()


def _bounded_ydl_options() -> dict[str, Any]:
    return {
        "logger": _RedactingYdlLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": YDL_SOCKET_TIMEOUT_SEC,
        "retries": YDL_RETRIES,
        "fragment_retries": YDL_RETRIES,
        "extractor_retries": YDL_RETRIES,
    }


def _enforce_download_bound(status: dict[str, Any]) -> None:
    observed = max(
        int(status.get("downloaded_bytes") or 0),
        int(status.get("total_bytes") or 0),
        int(status.get("total_bytes_estimate") or 0),
    )
    if observed > MAX_AUDIO_BYTES:
        raise AudioFetchError(
            f"Audio download exceeded the {MAX_AUDIO_BYTES // (1024 * 1024)} MiB limit."
        )


# ---------------------------------------------------------------------------
# Smart search
# ---------------------------------------------------------------------------


def _quarter_label(quarter: int) -> str:
    return f"Q{quarter}"


def _score_candidate(entry: dict[str, Any], ticker: str, year: int, quarter: int) -> int | None:
    """Score a yt-dlp search entry. None means reject.

    Score is positive integer; higher is better. Reject (None) on:
      - duration outside MIN/MAX band
      - title missing ticker, quarter token, or year
      - title contains any negative token (analyst recaps etc.)
    """
    title_raw = entry.get("title") or ""
    title = title_raw.lower()
    duration = entry.get("duration")
    if not isinstance(duration, (int, float)):
        return None
    if duration < MIN_DURATION_SEC or duration > MAX_DURATION_SEC:
        return None

    qtok = _quarter_label(quarter).lower()
    if ticker.lower() not in title:
        return None
    if qtok not in title:
        return None
    if str(year) not in title:
        return None

    if any(neg in title for neg in NEGATIVE_TITLE_TOKENS):
        return None

    score = 0
    for pos in POSITIVE_TITLE_TOKENS:
        if pos in title:
            score += 10
    # Prefer durations near 60 min (median for earnings calls).
    score += max(0, 10 - abs(int(duration) - 3600) // 300)
    return score


def smart_search_url(ticker: str, year: int, quarter: int, ffmpeg_location: Path | None) -> str:
    """Return the best matching YouTube URL via ytsearch5 + scoring. Raises if none qualify."""
    _enforce_audio_policy()
    query = f"ytsearch5:{ticker} {_quarter_label(quarter)} {year} earnings conference call"
    opts = _bounded_ydl_options()
    opts.update({"skip_download": True, "extract_flat": False})
    if ffmpeg_location is not None:
        opts["ffmpeg_location"] = str(ffmpeg_location)

    try:
        with yt_dlp.YoutubeDL(cast("Any", opts)) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as exc:
        raise AudioFetchError(
            f"YouTube search failed: {type(exc).__name__}: {_safe_external_message(exc)}"
        ) from None

    entries = info.get("entries")
    if not entries:
        raise RuntimeError(f"No YouTube results for {ticker} {_quarter_label(quarter)} {year}")

    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        typed_entry = cast("dict[str, Any]", entry)
        score = _score_candidate(typed_entry, ticker, year, quarter)
        if score is not None:
            scored.append((score, typed_entry))

    if not scored:
        raise RuntimeError(
            f"No qualifying candidates for {ticker} {_quarter_label(quarter)} {year}. "
            f"Inspect ytsearch5 output and curate a URL into the links manifest."
        )

    scored.sort(key=lambda t: t[0], reverse=True)
    best = scored[0][1]
    url = best.get("webpage_url") or best.get("url")
    if not url:
        raise RuntimeError(f"Best candidate has no URL: {best.get('id')}")
    safe_url = _validate_audio_url(str(url))
    print(
        f"[search] picked '{best.get('title')}' ({int(best.get('duration', 0))}s) "
        f"from {_safe_url_label(safe_url)}"
    )
    return safe_url


# ---------------------------------------------------------------------------
# Download + transcribe
# ---------------------------------------------------------------------------


def _resolve_ffmpeg_location(cli_value: str | None) -> Path | None:
    if cli_value:
        p = Path(cli_value)
        if not p.exists():
            raise FileNotFoundError(f"--ffmpeg-location does not exist: {p}")
        return p
    env_value = os.environ.get("FFMPEG_LOCATION")
    if env_value:
        p = Path(env_value)
        if not p.exists():
            raise FileNotFoundError(f"FFMPEG_LOCATION does not exist: {p}")
        return p
    if os.name == "nt" and WINDOWS_FFMPEG_DEFAULT.exists():
        return WINDOWS_FFMPEG_DEFAULT
    return None  # rely on PATH


def _download_audio(url: str, dest_stem: Path, ffmpeg_location: Path | None) -> Path:
    """Download audio to dest_stem.<ext>; return the actual produced file path."""
    _enforce_audio_policy()
    safe_url = _validate_audio_url(url)
    opts = _bounded_ydl_options()
    opts.update(
        {
            "format": "bestaudio/best",
            "outtmpl": str(dest_stem) + ".%(ext)s",
            "max_filesize": MAX_AUDIO_BYTES,
            "progress_hooks": [_enforce_download_bound],
        }
    )
    if ffmpeg_location is not None:
        opts["ffmpeg_location"] = str(ffmpeg_location)

    try:
        with yt_dlp.YoutubeDL(cast("Any", opts)) as ydl:
            ydl.download([safe_url])
    except AudioFetchError:
        raise
    except Exception as exc:
        raise AudioFetchError(
            f"Audio download failed: {type(exc).__name__}: {_safe_external_message(exc)}"
        ) from None

    pattern = re.compile(re.escape(dest_stem.name) + r"\.[A-Za-z0-9]+$")
    candidates = [p for p in dest_stem.parent.iterdir() if pattern.match(p.name)]
    if not candidates:
        raise FileNotFoundError(f"yt-dlp produced no file at {dest_stem}.*")
    if len(candidates) > 1:
        # Prefer the largest (in case of leftover .part files).
        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


def _transcribe(
    audio_path: Path,
    output_path: Path,
    model_name: str,
    beam_size: int,
) -> None:
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(audio_path), beam_size=beam_size)
    lines = [f"[{seg.start:.2f}s -> {seg.end:.2f}s] {seg.text}" for seg in segments]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _ensure_qa_recorded(
    canonical_ticker: str,
    year: int,
    qlabel: str,
    output_path: Path,
) -> str:
    """For an existing transcript file, ensure index + QA are recorded.
    Returns the qa_status string ('ok' or 'failed'). Idempotent — if the
    index already shows ok/failed AND has_qa is set, it's a no-op."""
    entry = cast(
        "dict[str, object] | None",
        index_manager.has_transcript(canonical_ticker, year, qlabel),
    )
    if entry is None:
        # File on disk but not in index — register with a stub source so
        # validate_transcript can route. Caller will overwrite with real source
        # if it just produced the file; backfill leaves "unknown_legacy".
        index_manager.register_transcript(
            canonical_ticker,
            year,
            qlabel,
            source="unknown_legacy",
            filepath=output_path.name,
            has_qa=None,
        )
        entry = cast(
            "dict[str, object] | None",
            index_manager.has_transcript(canonical_ticker, year, qlabel),
        )
        if entry is None:
            raise RuntimeError(f"failed to register existing transcript {output_path}")

    # Backfill has_qa if missing — legacy entries from before Q&A detection landed.
    if entry.get("has_qa") is None:
        try:
            section = detect_qa_section(output_path.read_text(encoding="utf-8"))
        except OSError:
            section = None
        if section is not None:
            index_manager.register_transcript(
                canonical_ticker,
                year,
                qlabel,
                source=str(entry.get("source") or "unknown_legacy"),
                filepath=output_path.name,
                has_qa=qa_status_to_db_value(section.status),
            )
            if section.status is QASectionStatus.ABSENT:
                sys.stderr.write(
                    f"WARN missing_qa: {canonical_ticker} {qlabel} {year} — "
                    f"existing transcript has no analyst Q&A signals "
                    f"(signals={list(section.signals)}); replace with a full-call "
                    f"source before running downstream extraction.\n"
                )

    qa_status_raw = entry.get("qa_status")
    qa_status = qa_status_raw if isinstance(qa_status_raw, str) else None
    if qa_status in ("ok", "failed"):
        return qa_status

    result = validate_transcript(output_path, str(entry.get("source") or "unknown_legacy"))
    index_manager.update_transcript_qa(
        canonical_ticker,
        year,
        qlabel,
        qa_status=result.status.value,
        qa_details=result.model_dump(mode="json"),
    )
    print(
        f"[qa] {canonical_ticker} {qlabel} {year}: {result.status.value} "
        f"(issues={len(result.issues)})"
    )
    for issue in result.issues:
        print(f"      - {issue}")
    return result.status.value


def fetch_and_transcribe(
    spec: FetchSpec,
    ffmpeg_location: Path | None,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    beam_size: int = DEFAULT_BEAM_SIZE,
    *,
    db_path: Path | None = None,
    owner_requested: bool = True,
    as_of: date | None = None,
) -> TranscriptionResult | None:
    canonical_ticker = resolve_ticker(spec.ticker)
    effective_db = PROJECT_ROOT / "data" / "portfolio.db" if db_path is None else db_path
    request = TranscriptAcquisitionRequest(
        entrypoint=TranscriptAcquisitionEntrypoint.FETCH_AUDIO_TRANSCRIPTS,
        canonical_ticker=canonical_ticker,
        fiscal_year=spec.year,
        fiscal_quarter=spec.quarter,
        as_of=date.today() if as_of is None else as_of,
        source_type=SourceType.TRANSCRIPT_AUDIO,
        document_type=DocType.EARNINGS_CALL_AUDIO,
        provider=TranscriptProvider.YOUTUBE_AUDIO,
        owner_requested=owner_requested,
        existing_artifact=False,
        existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
        source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
        source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
    )
    with connect_sqlite(
        effective_db,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=False,
    ) as conn:
        authorization = authorize_transcript_request(conn, request)
    _enforce_audio_policy(authorization)
    qlabel = _quarter_label(spec.quarter)
    output_path = RAW_DIR / f"{canonical_ticker}_{qlabel}_{spec.year}.txt"

    # Skip-existing logic, gated on QA: if the file is on disk we don't redo
    # work; we only run QA (once) so the index reflects reality.
    if output_path.exists() or index_manager.has_transcript(canonical_ticker, spec.year, qlabel):
        if output_path.exists():
            qa_status = _ensure_qa_recorded(canonical_ticker, spec.year, qlabel, output_path)
            if qa_status == "ok":
                print(f"[skip] {canonical_ticker} {qlabel} {spec.year}: transcript present, qa=ok")
            else:
                print(
                    f"[skip-failed-qa] {canonical_ticker} {qlabel} {spec.year}: "
                    f"transcript present but qa=failed; delete the file to retry"
                )
        else:
            print(
                f"[skip] {canonical_ticker} {qlabel} {spec.year}: indexed but file missing — "
                f"clear the index entry to retry"
            )
        return None

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    if spec.url is not None:
        url = _validate_audio_url(str(spec.url))
        source = TranscriptSource.YT_DLP_WHISPER_URL
        print(
            f"[{canonical_ticker} {qlabel} {spec.year}] "
            f"Using curated source: {_safe_url_label(url)}"
        )
    else:
        url = smart_search_url(spec.ticker, spec.year, spec.quarter, ffmpeg_location)
        source = TranscriptSource.YT_DLP_WHISPER_SEARCH

    audio_stem = TMP_DIR / f"temp_audio_{canonical_ticker}_{qlabel}_{spec.year}"
    # If a leftover from a prior aborted run exists (any extension), wipe it.
    for leftover in TMP_DIR.glob(audio_stem.name + ".*"):
        leftover.unlink()
    audio_path = _download_audio(url, audio_stem, ffmpeg_location)

    print(
        f"[{canonical_ticker} {qlabel} {spec.year}] Transcribing {audio_path.name} "
        f"(model={whisper_model}, beam_size={beam_size})…"
    )
    _transcribe(audio_path, output_path, whisper_model, beam_size)

    # Two independent QA checks against the just-produced transcript:
    #   (a) structural validity — file size, timestamps, words/sec, hallucination
    #       repeat ratio. Gates the audio-cache cleanup (failed transcripts keep
    #       the cached audio so the user can rerun with different decode params
    #       without re-downloading).
    #   (b) Q&A-section presence — was analyst Q&A in the recording at all,
    #       or did the source cut off at the hand-off? A structurally-OK file
    #       can still be prepared-remarks only; downstream Say-Do / commitments
    #       extraction needs to know.
    qa_result = validate_audio_transcript(output_path)
    qa_details_payload = qa_result.model_dump(mode="json")
    qa_section = detect_qa_section(output_path.read_text(encoding="utf-8"))
    if qa_section.status is QASectionStatus.ABSENT:
        sys.stderr.write(
            f"WARN missing_qa: {canonical_ticker} {qlabel} {spec.year} — "
            f"transcribed audio has no analyst Q&A signals "
            f"(signals={list(qa_section.signals)}). Likely the source cut off "
            f"at hand-off; provide a full-call replacement or pull a longer "
            f"recording before running Say-Do/commitments mining.\n"
        )

    index_manager.register_transcript(
        canonical_ticker,
        spec.year,
        qlabel,
        source=source.value,
        filepath=output_path.name,
        has_qa=qa_status_to_db_value(qa_section.status),
        qa_status=qa_result.status.value,
        qa_details=qa_details_payload,
    )

    if qa_result.status == QaStatus.OK:
        audio_path.unlink(missing_ok=True)
        print(f"[done] {output_path}  qa=ok  has_qa={qa_section.status.value}  audio_cleaned")
    else:
        print(
            f"[done-qa-failed] {output_path}  qa=failed  "
            f"has_qa={qa_section.status.value}  audio_kept={audio_path.name}  "
            f"issues={len(qa_result.issues)}"
        )
        for issue in qa_result.issues:
            print(f"      - {issue}")

    return TranscriptionResult(
        ticker=canonical_ticker,
        year=spec.year,
        quarter=spec.quarter,
        output_path=output_path,
        source=source,
        video_url=url,
        qa_section_status=qa_section.status,
        qa_section_signals=qa_section.signals,
    )


# ---------------------------------------------------------------------------
# Manifest mode
# ---------------------------------------------------------------------------


def _load_manifest(path: Path) -> LinksManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Tolerate top-level _meta keys; only `links` is required.
    return LinksManifest(links=raw["links"])


def run_from_manifest(
    manifest_path: Path,
    ffmpeg_location: Path | None,
    ticker_filter: str | None,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    beam_size: int = DEFAULT_BEAM_SIZE,
) -> list[TranscriptionResult]:
    manifest = _load_manifest(manifest_path)
    results: list[TranscriptionResult] = []
    skipped_gaps: list[LinksManifestEntry] = []

    target_ticker = resolve_ticker(ticker_filter).upper() if ticker_filter else None

    for entry in manifest.links:
        canonical = resolve_ticker(entry.ticker).upper()
        if target_ticker and canonical != target_ticker:
            continue
        if entry.url is None:
            skipped_gaps.append(entry)
            print(
                f"[gap] {canonical} Q{entry.quarter} {entry.year}: {entry.gap_reason or 'no URL'}"
            )
            continue
        spec = FetchSpec(
            ticker=entry.ticker,
            year=entry.year,
            quarter=entry.quarter,
            url=entry.url,
        )
        result = fetch_and_transcribe(spec, ffmpeg_location, whisper_model, beam_size)
        if result is not None:
            results.append(result)

    missing_qa = [
        f"{r.ticker} Q{r.quarter} {r.year}"
        for r in results
        if r.qa_section_status is QASectionStatus.ABSENT
    ]
    print(
        f"[summary] processed={len(results)} "
        f"gaps_skipped={len(skipped_gaps)} "
        f"missing_qa={len(missing_qa)} "
        f"manifest={manifest_path.name}"
    )
    if missing_qa:
        print(
            "[action_required] These transcripts have no Q&A section and need a full-call "
            "replacement before downstream extraction can use them:"
        )
        for label in missing_qa:
            print(f"  - {label}")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch earnings call audio and transcribe locally."
    )
    parser.add_argument("--ticker", help="Stock ticker symbol (single-quarter mode)")
    parser.add_argument("--year", type=int, help="Year (single-quarter mode)")
    parser.add_argument(
        "--quarter", type=int, choices=[1, 2, 3, 4], help="Quarter (single-quarter mode)"
    )
    parser.add_argument(
        "--url", help="Explicit YouTube URL (skips search). Requires --ticker/--year/--quarter."
    )
    parser.add_argument(
        "--links-file",
        help="JSON manifest path (batch mode). Mutually exclusive with --url.",
    )
    parser.add_argument(
        "--only-ticker",
        help="When using --links-file, only process this ticker.",
    )
    parser.add_argument(
        "--ffmpeg-location",
        help="Directory containing ffmpeg.exe. Falls back to FFMPEG_LOCATION env, "
        "then C:/ffmpeg/bin on Windows, then PATH.",
    )
    parser.add_argument(
        "--whisper-model",
        default=os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL),
        help=f"faster-whisper model name (default: {DEFAULT_WHISPER_MODEL}; "
        f"set WHISPER_MODEL env to override).",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=int(os.environ.get("WHISPER_BEAM_SIZE", DEFAULT_BEAM_SIZE)),
        help=f"Whisper decode beam size (default: {DEFAULT_BEAM_SIZE}; "
        f"set WHISPER_BEAM_SIZE env to override). Greedy=1; raise for accuracy.",
    )
    args = parser.parse_args()

    ffmpeg_location = _resolve_ffmpeg_location(args.ffmpeg_location)

    if args.links_file:
        if args.url or args.ticker or args.year or args.quarter:
            parser.error("--links-file is mutually exclusive with single-quarter args.")
        run_from_manifest(
            Path(args.links_file),
            ffmpeg_location,
            args.only_ticker,
            args.whisper_model,
            args.beam_size,
        )
        return

    if not (args.ticker and args.year and args.quarter):
        parser.error("Single-quarter mode requires --ticker, --year, --quarter.")

    try:
        spec = FetchSpec(
            ticker=args.ticker,
            year=args.year,
            quarter=args.quarter,
            url=args.url,
        )
    except ValidationError as e:
        parser.error(str(e))

    fetch_and_transcribe(spec, ffmpeg_location, args.whisper_model, args.beam_size)


if __name__ == "__main__":
    main()

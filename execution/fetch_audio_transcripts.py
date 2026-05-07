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
from enum import Enum
from pathlib import Path
from typing import Any

import yt_dlp
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.append(str(SRC_DIR))

from alias_manager import resolve_ticker  # noqa: E402
import index_manager  # noqa: E402
from transcript_qa import (  # noqa: E402
    QaStatus,
    validate_audio_transcript,
    validate_transcript,
)

RAW_DIR = PROJECT_ROOT / "transcripts" / "raw"
TMP_DIR = PROJECT_ROOT / ".tmp"

# Earnings calls: typical 30–110 min; reject anything outside this band.
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


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TranscriptSource(str, Enum):
    YT_DLP_WHISPER_URL = "yt_dlp_whisper_url"          # explicit URL provided
    YT_DLP_WHISPER_SEARCH = "yt_dlp_whisper_search"    # picked via smart search
    YT_DLP_WHISPER_LINKS = "yt_dlp_whisper_links"      # picked from links file


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
    query = f"ytsearch5:{ticker} {_quarter_label(quarter)} {year} earnings conference call"
    opts: dict[str, Any] = {"quiet": True, "skip_download": True, "extract_flat": False}
    if ffmpeg_location is not None:
        opts["ffmpeg_location"] = str(ffmpeg_location)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        raise RuntimeError(f"No YouTube results for {ticker} {_quarter_label(quarter)} {year}")

    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        score = _score_candidate(entry, ticker, year, quarter)
        if score is not None:
            scored.append((score, entry))

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
    print(f"[search] picked '{best.get('title')}' ({int(best.get('duration', 0))}s) — {url}")
    return url


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
    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_stem) + ".%(ext)s",
        "quiet": False,
        "noprogress": False,
    }
    if ffmpeg_location is not None:
        opts["ffmpeg_location"] = str(ffmpeg_location)

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

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
    lines = [
        f"[{seg.start:.2f}s -> {seg.end:.2f}s] {seg.text}"
        for seg in segments
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _ensure_qa_recorded(
    canonical_ticker: str,
    year: int,
    qlabel: str,
    output_path: Path,
) -> str:
    """For an existing transcript file, ensure index + QA are recorded.
    Returns the qa_status string ('ok' or 'failed'). Idempotent — if the
    index already shows ok/failed, it's a no-op."""
    entry = index_manager.has_transcript(canonical_ticker, year, qlabel)
    if entry is None:
        # File on disk but not in index — register with a stub source so
        # validate_transcript can route. Caller will overwrite with real source
        # if it just produced the file; backfill leaves "unknown_legacy".
        index_manager.register_transcript(
            canonical_ticker, year, qlabel,
            source="unknown_legacy",
            filepath=output_path.name,
            has_qa=None,
        )
        entry = index_manager.has_transcript(canonical_ticker, year, qlabel)
        if entry is None:
            raise RuntimeError(f"failed to register existing transcript {output_path}")

    qa_status = entry.get("qa_status")
    if qa_status in ("ok", "failed"):
        return qa_status

    result = validate_transcript(output_path, entry.get("source") or "unknown_legacy")
    index_manager.update_transcript_qa(
        canonical_ticker, year, qlabel,
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
) -> TranscriptionResult | None:
    canonical_ticker = resolve_ticker(spec.ticker)
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
        url = str(spec.url)
        source = TranscriptSource.YT_DLP_WHISPER_URL
        print(f"[{canonical_ticker} {qlabel} {spec.year}] Using curated URL: {url}")
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

    # QA gate: validate the just-produced transcript. Cached audio is only
    # deleted on QA pass — failed transcripts keep the audio so the user can
    # rerun with a different model / beam_size without re-downloading.
    qa_result = validate_audio_transcript(output_path)
    qa_details_payload = qa_result.model_dump(mode="json")
    index_manager.register_transcript(
        canonical_ticker,
        spec.year,
        qlabel,
        source=source.value,
        filepath=output_path.name,
        has_qa=None,
        qa_status=qa_result.status.value,
        qa_details=qa_details_payload,
    )

    if qa_result.status == QaStatus.OK:
        audio_path.unlink(missing_ok=True)
        print(f"[done] {output_path}  qa=ok  audio_cleaned")
    else:
        print(
            f"[done-qa-failed] {output_path}  qa=failed  "
            f"audio_kept={audio_path.name}  issues={len(qa_result.issues)}"
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

    print(
        f"[summary] processed={len(results)} "
        f"gaps_skipped={len(skipped_gaps)} "
        f"manifest={manifest_path.name}"
    )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch earnings call audio and transcribe locally.")
    parser.add_argument("--ticker", help="Stock ticker symbol (single-quarter mode)")
    parser.add_argument("--year", type=int, help="Year (single-quarter mode)")
    parser.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], help="Quarter (single-quarter mode)")
    parser.add_argument("--url", help="Explicit YouTube URL (skips search). Requires --ticker/--year/--quarter.")
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

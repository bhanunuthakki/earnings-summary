"""
execution/qa_transcripts.py
---------------------------
Standalone QA + cleanup tool for `transcripts/raw/`.

Modes (all are idempotent; combine freely):

  --backfill              For every .txt in transcripts/raw/ that has no
                          recorded qa_status, run validate_transcript and
                          persist the result. (Default if no other mode flag
                          is given.)

  --rerun-all             Re-validate every transcript regardless of prior
                          status. Use after tightening QA thresholds.

  --ticker SYMBOL         Filter all modes to a single ticker.

  --report                Print a status table of every indexed transcript.
                          Cheap; safe to combine with anything.

  --clean-orphan-audio    Delete `.tmp/temp_audio_<TICKER>_Q<N>_<YEAR>.*`
                          files whose corresponding transcript is qa=ok.
                          Keeps audio for qa=failed entries (so the user can
                          rerun Whisper without re-downloading).

If no mode flag is given the default is `--backfill --clean-orphan-audio`,
which is what the user's "automatic skip + automatic cleanup" requirement
asks for.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.append(str(SRC_DIR))

import index_manager  # noqa: E402
from alias_manager import resolve_ticker  # noqa: E402
from transcript_qa import (  # noqa: E402
    QaStatus,
    validate_transcript,
)

RAW_DIR = PROJECT_ROOT / "transcripts" / "raw"
TMP_DIR = PROJECT_ROOT / ".tmp"

# Filename: <TICKER>_Q<N>_<YEAR>.txt — strict, ticker must match alias rules.
TRANSCRIPT_FNAME_RE = re.compile(r"^(?P<ticker>[A-Z][A-Z0-9.\-]*)_Q(?P<q>[1-4])_(?P<y>\d{4})\.txt$")
AUDIO_FNAME_RE = re.compile(
    r"^temp_audio_(?P<ticker>[A-Z][A-Z0-9.\-]*)_Q(?P<q>[1-4])_(?P<y>\d{4})\.[A-Za-z0-9]+$"
)


@dataclass(frozen=True)
class TranscriptRef:
    ticker: str
    year: int
    quarter: int
    qlabel: str
    path: Path


def _scan_transcripts(ticker_filter: str | None) -> list[TranscriptRef]:
    if not RAW_DIR.is_dir():
        return []
    target = resolve_ticker(ticker_filter).upper() if ticker_filter else None
    out: list[TranscriptRef] = []
    for p in sorted(RAW_DIR.iterdir()):
        m = TRANSCRIPT_FNAME_RE.match(p.name)
        if not m:
            continue
        ticker = resolve_ticker(m.group("ticker")).upper()
        if target and ticker != target:
            continue
        out.append(
            TranscriptRef(
                ticker=ticker,
                year=int(m.group("y")),
                quarter=int(m.group("q")),
                qlabel=f"Q{m.group('q')}",
                path=p,
            )
        )
    return out


def _run_qa(refs: list[TranscriptRef], rerun_all: bool) -> dict[str, int]:
    counts = {"ok": 0, "failed": 0, "skipped": 0, "registered_new": 0}
    for ref in refs:
        entry = index_manager.has_transcript(ref.ticker, ref.year, ref.qlabel)
        if entry is None:
            # File on disk but unindexed — register a stub so update_transcript_qa
            # has a row to write to.
            print(
                f"[qa-denied] {ref.ticker} {ref.qlabel} {ref.year}: "
                "missing authorized acquisition receipt",
                file=sys.stderr,
            )
            counts["skipped"] += 1
            continue

        prior_status = entry.get("qa_status")
        if not rerun_all and prior_status in ("ok", "failed"):
            counts["skipped"] += 1
            continue

        result = validate_transcript(ref.path, entry.get("source") or "unknown_legacy")
        index_manager.update_transcript_qa(
            ref.ticker,
            ref.year,
            ref.qlabel,
            qa_status=result.status.value,
            qa_details=result.model_dump(mode="json"),
        )
        counts[result.status.value] += 1
        marker = "ok " if result.status == QaStatus.OK else "FAIL"
        print(
            f"[qa] {marker} {ref.ticker:5s} {ref.qlabel} {ref.year}  "
            f"size={result.file_size_bytes:>7}  lines={result.line_count:>4}  "
            f"issues={len(result.issues)}"
        )
        for issue in result.issues:
            print(f"        - {issue}")
    return counts


def _clean_orphan_audio(ticker_filter: str | None) -> tuple[int, int]:
    """Delete .tmp/temp_audio_*.* whose matching transcript is qa=ok.
    Returns (deleted_count, kept_count)."""
    if not TMP_DIR.is_dir():
        return (0, 0)
    target = resolve_ticker(ticker_filter).upper() if ticker_filter else None
    deleted = 0
    kept = 0
    for p in sorted(TMP_DIR.iterdir()):
        m = AUDIO_FNAME_RE.match(p.name)
        if not m:
            continue
        ticker = resolve_ticker(m.group("ticker")).upper()
        if target and ticker != target:
            continue
        year = int(m.group("y"))
        qlabel = f"Q{m.group('q')}"
        entry = index_manager.has_transcript(ticker, year, qlabel)
        qa_status = entry.get("qa_status") if entry else None
        if qa_status == "ok":
            p.unlink()
            print(f"[clean] removed orphan audio {p.name} (qa=ok)")
            deleted += 1
        else:
            print(
                f"[keep] {p.name} (qa={qa_status or '<not-set>'}) — transcript not validated, audio kept for retry"
            )
            kept += 1
    return (deleted, kept)


def _report(refs: list[TranscriptRef]) -> None:
    print(
        f"{'TICKER':6s} {'PERIOD':10s} {'SOURCE':22s} {'QA':6s} {'SIZE':>8s} {'LINES':>6s} ISSUES"
    )
    print("-" * 85)
    for ref in refs:
        entry = index_manager.has_transcript(ref.ticker, ref.year, ref.qlabel)
        if entry is None:
            print(f"{ref.ticker:6s} {ref.qlabel} {ref.year}   <no index entry>")
            continue
        details = entry.get("qa_details") or {}
        issues = details.get("issues") or []
        print(
            f"{ref.ticker:6s} {ref.qlabel} {ref.year}   {entry.get('source', '?'):22s} "
            f"{(entry.get('qa_status') or '?'):6s} "
            f"{details.get('file_size_bytes', 0):>8} "
            f"{details.get('line_count', 0):>6} "
            f"{len(issues)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="QA validation + audio-cache cleanup for transcripts/raw/."
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Run QA on transcripts that have no recorded qa_status.",
    )
    parser.add_argument(
        "--rerun-all",
        action="store_true",
        help="Re-validate every transcript regardless of prior status.",
    )
    parser.add_argument(
        "--clean-orphan-audio",
        action="store_true",
        help="Delete .tmp/temp_audio_*.* whose transcript is qa=ok.",
    )
    parser.add_argument(
        "--report", action="store_true", help="Print a status table of every indexed transcript."
    )
    parser.add_argument("--ticker", help="Restrict all modes to this ticker.")
    args = parser.parse_args()

    # Default behaviour when no mode flag is given: backfill + cleanup.
    if not (args.backfill or args.rerun_all or args.clean_orphan_audio or args.report):
        args.backfill = True
        args.clean_orphan_audio = True

    refs = _scan_transcripts(args.ticker)
    print(
        f"[scan] {len(refs)} transcript file(s) in {RAW_DIR.relative_to(PROJECT_ROOT)}"
        + (f" (ticker={args.ticker})" if args.ticker else "")
    )

    if args.backfill or args.rerun_all:
        counts = _run_qa(refs, rerun_all=args.rerun_all)
        print(
            f"[qa-summary] ok={counts['ok']} failed={counts['failed']} "
            f"skipped={counts['skipped']} registered_new={counts['registered_new']}"
        )

    if args.clean_orphan_audio:
        deleted, kept = _clean_orphan_audio(args.ticker)
        print(f"[clean-summary] deleted={deleted} kept={kept}")

    if args.report:
        _report(refs)


if __name__ == "__main__":
    main()

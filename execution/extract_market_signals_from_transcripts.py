"""
execution/extract_market_signals_from_transcripts.py
-----------------------------------------------------
Layer 3 CLI: Extract structured market signals (script volume, price/unit,
share, manufacturing capacity, patent commentary, pipeline milestones) from
NVO + LLY earnings call transcripts.

Reads transcripts from `transcripts/processed/` matching `<TICKER>_Q<N>_<YYYY>.txt`,
runs Gemini structured-output extraction, writes one bundle JSON per ticker
per period to .tmp/glp1_market_signals/.

Idempotent: skips if today's run for the same (ticker, period) is already on disk.

Usage:
    python execution/extract_market_signals_from_transcripts.py --ticker NVO --period 2026Q1
    python execution/extract_market_signals_from_transcripts.py --tickers NVO,LLY --all-periods
    python execution/extract_market_signals_from_transcripts.py --tickers NVO,LLY --all-periods --force
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import google.generativeai as genai  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from models.patents import (  # noqa: E402
    Confidence,
    MarketSignal,
    MarketSignalBundle,
    SignalType,
)
from parser import read_text_file  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

PROCESSED_DIR = PROJECT_ROOT / "transcripts" / "processed"
OUT_DIR = PROJECT_ROOT / ".tmp" / "glp1_market_signals"

DEFAULT_TICKERS: list[str] = ["NVO", "LLY"]

FILENAME_RX = re.compile(r"^(?P<ticker>[A-Z]+)_Q(?P<q>[1-4])_(?P<year>\d{4})\.(?:txt|pdf)$")

EXTRACTION_PROMPT = """You are extracting GLP-1 / obesity / diabetes market signals from an earnings call transcript.

The thesis (NVO portfolio holding) depends on signals NOT directly visible in headline financials:
- script_volume: TRx / NRx volume mentions, units shipped, doses dispensed (NOT revenue)
- price_per_unit: net realized price per dose, list-vs-net price, MFN pricing impact
- market_share: market share by molecule (semaglutide, tirzepatide, retatrutide) in diabetes or obesity, by geography
- manufacturing_capacity: production capacity adds, fill-finish constraints, supply-demand framing
- patent_commentary: LOE timing, extension strategy, generic competition discussion
- pipeline_milestone: trial readouts, NDA/BLA filings, FDA decisions for next-gen molecules

For each signal you find, extract:
- signal_type: one of [script_volume, price_per_unit, market_share, manufacturing_capacity, patent_commentary, pipeline_milestone]
- molecule: e.g. "semaglutide", "tirzepatide", "retatrutide", "cagrisema", "liraglutide" (lowercase) — null if not molecule-specific
- geography: e.g. "US", "EU", "Global", "International" — null if not specified
- metric_text: the verbatim quote (under 400 chars)
- parsed_value: numeric value if mentioned (e.g. "+15%" -> 15.0); null if not numeric
- parsed_unit: "%", "doses_per_week", "USD_per_dose", "scripts_per_week", etc; null if not applicable
- direction: "up", "down", "flat", null
- confidence: "high" if explicitly stated by management, "medium" if implied, "low" if speculative

Skip signals that are:
- Pure revenue / op profit numbers (those are in the financial statements; not market signals)
- Employee headcount / SBC / corporate overhead
- Mentions without specifics (e.g. "we continue to see strong demand" with no numeric or directional content)

Return JSON: {"signals": [...]}. Empty list is valid if no qualifying signals found.

Transcript:
"""

GENERATION_CONFIG: dict[str, Any] = {
    "response_mime_type": "application/json",
    "temperature": 0.1,
}


def _log(event: str, **kwargs: Any) -> None:
    payload = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **kwargs}
    sys.stderr.write(json.dumps(payload) + "\n")


def discover_transcripts(tickers: list[str]) -> list[tuple[str, str, Path]]:
    """Return list of (ticker, period, path) for matching transcripts."""
    if not PROCESSED_DIR.exists():
        return []
    out: list[tuple[str, str, Path]] = []
    upper_tickers = {t.upper() for t in tickers}
    for entry in PROCESSED_DIR.iterdir():
        if not entry.is_file():
            continue
        m = FILENAME_RX.match(entry.name)
        if m is None:
            continue
        ticker = m.group("ticker")
        if ticker not in upper_tickers:
            continue
        period = f"{m.group('year')}Q{m.group('q')}"
        out.append((ticker, period, entry))
    return out


def filter_to_period(
    transcripts: list[tuple[str, str, Path]],
    period: str | None,
) -> list[tuple[str, str, Path]]:
    if period is None:
        return transcripts
    return [t for t in transcripts if t[1] == period]


def output_path(ticker: str, period: str) -> Path:
    return OUT_DIR / f"{ticker}_{period}.json"


def extract_signals(
    ticker: str,
    period: str,
    transcript_path: Path,
) -> MarketSignalBundle:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set; cannot run LLM extraction.")
    text = read_text_file(str(transcript_path))
    if not text.strip():
        raise RuntimeError(f"Transcript empty: {transcript_path}")

    model = genai.GenerativeModel("gemini-flash-latest")
    response = model.generate_content(
        EXTRACTION_PROMPT + text[:120_000],
        generation_config=GENERATION_CONFIG,
    )
    raw = response.text.strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "signals" not in payload:
        raise ValueError(f"LLM returned unexpected schema: {payload!r}")
    raw_signals = payload["signals"]
    if not isinstance(raw_signals, list):
        raise ValueError(f"'signals' field is not a list: {type(raw_signals)}")

    extracted_at = datetime.utcnow()
    signals: list[MarketSignal] = []
    for raw_sig in raw_signals:
        signals.append(_parse_signal(raw_sig, ticker, period, transcript_path.name, extracted_at))

    return MarketSignalBundle(
        ticker=ticker,
        period=period,
        transcript_filename=transcript_path.name,
        extracted_at=extracted_at,
        signals=signals,
    )


def _parse_signal(
    raw: dict[str, Any],
    ticker: str,
    period: str,
    transcript_filename: str,
    extracted_at: datetime,
) -> MarketSignal:
    mol = raw.get("molecule")
    molecule = str(mol).lower() if mol else None
    return MarketSignal(
        ticker=ticker,
        period=period,
        signal_type=SignalType(raw["signal_type"]),
        molecule=molecule,
        geography=raw.get("geography") or None,
        metric_text=str(raw.get("metric_text", "")),
        parsed_value=raw.get("parsed_value"),
        parsed_unit=raw.get("parsed_unit") or None,
        direction=raw.get("direction") or None,
        confidence=Confidence(raw.get("confidence", "low")),
        transcript_filename=transcript_filename,
        extracted_at=extracted_at,
    )


def run_extraction(
    transcripts: list[tuple[str, str, Path]],
    force: bool,
) -> list[dict[str, Any]]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for ticker, period, path in transcripts:
        out_path = output_path(ticker, period)
        if out_path.exists() and not force:
            _log("idempotent_skip", ticker=ticker, period=period, path=str(out_path))
            continue
        _log("extracting", ticker=ticker, period=period, transcript=str(path))
        bundle = extract_signals(ticker, period, path)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(bundle.model_dump(mode="json"), f, indent=2)
        summaries.append(
            {
                "ticker": ticker,
                "period": period,
                "signal_count": len(bundle.signals),
                "output_file": str(out_path.relative_to(PROJECT_ROOT)),
            }
        )
        _log("done", ticker=ticker, period=period, signal_count=len(bundle.signals))
    return summaries


def parse_tickers(value: str | None) -> list[str]:
    if not value:
        return DEFAULT_TICKERS
    return [t.strip().upper() for t in value.split(",") if t.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract GLP-1 market signals from transcripts.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ticker", type=str, help="Single ticker (e.g. NVO)")
    group.add_argument(
        "--tickers",
        type=str,
        help="Comma-separated tickers (default: NVO,LLY)",
    )
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument("--period", type=str, help="Single period (e.g. 2026Q1)")
    period_group.add_argument("--all-periods", action="store_true", help="Process all available periods")
    parser.add_argument("--force", action="store_true", help="Re-extract even if output file exists")
    args = parser.parse_args()

    tickers = [args.ticker.upper()] if args.ticker else parse_tickers(args.tickers)
    transcripts = discover_transcripts(tickers)
    if not transcripts:
        _log("no_transcripts_found", tickers=tickers, processed_dir=str(PROCESSED_DIR))
        sys.stderr.write(
            f"No transcripts found in {PROCESSED_DIR} for tickers {tickers}. "
            "Fetch transcripts first via execution/fetch_audio_transcripts.py.\n"
        )
        sys.exit(1)

    if not args.all_periods:
        transcripts = filter_to_period(transcripts, args.period)
        if not transcripts:
            sys.stderr.write(f"No transcripts match period {args.period}.\n")
            sys.exit(1)

    summaries = run_extraction(transcripts, force=args.force)
    sys.stdout.write(
        json.dumps({
            "tickers": tickers,
            "extracted": len(summaries),
            "skipped": len(transcripts) - len(summaries),
            "results": summaries,
        })
        + "\n"
    )


if __name__ == "__main__":
    main()

"""
execution/extract_nvo_patent_timeline.py
-----------------------------------------
Layer 3 CLI: Extract NVO's self-disclosed patent expiry timeline from the
latest annual report PDF.

Reads `ir_documents/NVO/<latest_year>_Q4/press_release.pdf` (or whichever
annual-report-mapped artifact is present), runs Gemini structured-output
extraction against the relevant patent / IP section, and writes a Pydantic-
validated timeline to .tmp/nvo_patents/.

Refresh cadence: annual (after each FY release). The script is idempotent —
re-running with the same source PDF + same run date is a no-op.

Usage:
    python execution/extract_nvo_patent_timeline.py
    python execution/extract_nvo_patent_timeline.py --pdf path/to/annual_report.pdf
    python execution/extract_nvo_patent_timeline.py --force
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from dotenv import load_dotenv  # noqa: E402

from llm_client import call_llm  # noqa: E402
from models.patents import NvoSelfDisclosedPatent, NvoSelfDisclosedTimeline  # noqa: E402
from parser import extract_text_from_pdf  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

OUT_DIR = PROJECT_ROOT / ".tmp" / "nvo_patents"
IR_DOCS_DIR = PROJECT_ROOT / "ir_documents" / "NVO"
SOURCES_DIR = PROJECT_ROOT / "micro_thesis" / "sources" / "NVO"

EXTRACTION_PROMPT = """You are extracting structured patent expiry data from Novo Nordisk's annual report.

Find the section discussing intellectual property, patent protection, and Loss of Exclusivity (LOE) timelines.
Focus on the GLP-1 franchise: semaglutide (Ozempic, Wegovy, Rybelsus), liraglutide (Victoza, Saxenda), and any next-gen molecules (CagriSema, oral semaglutide for obesity).

For each molecule discussed, extract:
- molecule (lowercase, e.g. "semaglutide")
- jurisdictions (list of country/region codes where patent expiry is discussed; use "US", "EU", "CN", "IN", "BR", "CA", "JP" — or "OTHER" if specified differently)
- expected_loe_year (integer year of expected loss of exclusivity, if stated)
- extension_strategy_text (one-sentence summary of any extension strategy mentioned, e.g. "pediatric exclusivity filing", "pediatric exclusivity expected to add 6 months in US")
- raw_text (the verbatim sentence(s) from the report — keep under 500 chars)

Return a JSON object with a single key "patents" containing a list of these records. If the section discusses no patent expiries by molecule, return {"patents": []}.

Document text:
"""

JSON_FENCE_RX = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _log(event: str, **kwargs: Any) -> None:
    payload = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **kwargs}
    sys.stderr.write(json.dumps(payload) + "\n")


def find_default_pdf() -> Path | None:
    """Find the latest NVO annual report PDF.

    Search order:
      1. ir_documents/NVO/<year>_Q4/press_release.pdf (latest year)
      2. micro_thesis/sources/NVO/*.pdf (any annual report bundled with sources)
    """
    candidates: list[Path] = []
    if IR_DOCS_DIR.exists():
        for year_dir in sorted(IR_DOCS_DIR.iterdir(), reverse=True):
            q4 = year_dir / "press_release.pdf"
            if q4.exists():
                candidates.append(q4)
                break  # Latest year only
    if SOURCES_DIR.exists():
        for pdf in SOURCES_DIR.glob("*.pdf"):
            if (
                "annual" in pdf.name.lower()
                or "10-k" in pdf.name.lower()
                or "fy" in pdf.name.lower()
            ):
                candidates.append(pdf)
    return candidates[0] if candidates else None


def infer_fiscal_year(pdf_path: Path) -> int:
    """Best-effort fiscal year from filename or parent directory."""
    name = pdf_path.name + " " + pdf_path.parent.name
    for token in name.split():
        if token.isdigit() and len(token) == 4 and 2000 <= int(token) <= 2099:
            return int(token)
    return datetime.utcnow().year - 1


def extract_timeline(pdf_path: Path) -> NvoSelfDisclosedTimeline:
    """Run LLM extraction over the PDF and return a validated timeline."""
    text = extract_text_from_pdf(str(pdf_path))
    if not text.strip():
        raise RuntimeError(f"No text extracted from {pdf_path}")

    # Cap to ~80k chars — extraction quality falls off when the relevant
    # section is buried; trim to first 80k. Routes through llm_client.call_llm
    # (Claude CLI subscription billing first, Gemini Flash fallback) — model
    # selected via LLM_MODELS["patent_timeline"] (Haiku for batch latency).
    capped = text[:80_000]

    raw = call_llm(EXTRACTION_PROMPT + capped, purpose="patent_timeline").strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RX.sub("", raw).strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "patents" not in payload:
        raise ValueError(f"LLM returned unexpected schema: {payload!r}")

    fiscal_year = infer_fiscal_year(pdf_path)
    extracted_at = datetime.utcnow()
    patents: list[NvoSelfDisclosedPatent] = []
    raw_patents = payload["patents"]
    if not isinstance(raw_patents, list):
        raise ValueError(f"'patents' field is not a list: {type(raw_patents)}")
    for entry in raw_patents:
        patents.append(
            NvoSelfDisclosedPatent(
                molecule=str(entry.get("molecule", "")).lower(),
                jurisdictions=[str(j).upper() for j in entry.get("jurisdictions", [])],
                expected_loe_year=entry.get("expected_loe_year"),
                extension_strategy_text=entry.get("extension_strategy_text"),
                raw_text=str(entry.get("raw_text", "")),
                source_filename=pdf_path.name,
                extracted_at=extracted_at,
            )
        )

    return NvoSelfDisclosedTimeline(
        fiscal_year=fiscal_year,
        source_filename=pdf_path.name,
        extracted_at=extracted_at,
        patents=patents,
    )


def write_output(timeline: NvoSelfDisclosedTimeline) -> Path:
    """Write the timeline to both (a) the dated history archive under .tmp
    and (b) the canonical ticker-specific path the bear_case prompt loads.

    The canonical file `data/ticker_specific/NVO/patent_timeline.json` is the
    one the brief generator reads (per the Phase 5 convention). The dated
    .tmp file is the audit trail.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_date = datetime.utcnow().strftime("%Y-%m-%d")
    payload = timeline.model_dump(mode="json")

    history_path = OUT_DIR / f"nvo_self_disclosed_{run_date}.json"
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    canonical_path = PROJECT_ROOT / "data" / "ticker_specific" / "NVO" / "patent_timeline.json"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    with open(canonical_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return canonical_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract NVO self-disclosed patent timeline.")
    parser.add_argument("--pdf", type=Path, default=None, help="Override path to annual report PDF")
    parser.add_argument(
        "--force", action="store_true", help="Re-extract even if today's file exists"
    )
    args = parser.parse_args()

    pdf_path = args.pdf or find_default_pdf()
    if pdf_path is None:
        _log("no_source_pdf", searched=[str(IR_DOCS_DIR), str(SOURCES_DIR)])
        sys.stderr.write("ERROR: No NVO annual report PDF found. Use --pdf to specify a path.\n")
        sys.exit(1)
    if not pdf_path.exists():
        sys.stderr.write(f"ERROR: PDF not found: {pdf_path}\n")
        sys.exit(1)

    run_date = datetime.utcnow().strftime("%Y-%m-%d")
    expected = OUT_DIR / f"nvo_self_disclosed_{run_date}.json"
    if expected.exists() and not args.force:
        _log("idempotent_skip", path=str(expected))
        sys.stdout.write(expected.read_text(encoding="utf-8"))
        sys.stdout.write("\n")
        return

    _log("extracting", pdf=str(pdf_path))
    timeline = extract_timeline(pdf_path)
    out_path = write_output(timeline)
    _log("done", path=str(out_path), patent_count=len(timeline.patents))
    sys.stdout.write(
        json.dumps(
            {
                "output_file": str(out_path.relative_to(PROJECT_ROOT)),
                "fiscal_year": timeline.fiscal_year,
                "patent_count": len(timeline.patents),
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

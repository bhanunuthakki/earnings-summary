"""
scratch/extract_dropped_pdfs.py
-------------------------------
One-shot utility: walks micro_thesis/sources/<TICKER>/, extracts text from every
PDF, deduplicates by file size, and writes:
  - .tmp/extracted/<TICKER>_<QUARTER>_<YEAR>_<DOC_TYPE>.txt
  - .tmp/extracted/manifest.json

Filename → (quarter, year, doc_type) inference uses explicit per-file overrides
for the known dropped corpus. Anything that doesn't match an override is reported
loudly rather than guessed — keeps the no-hallucination contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict

from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "micro_thesis" / "sources"
OUT_DIR = PROJECT_ROOT / ".tmp" / "extracted"


class FileSpec(TypedDict):
    quarter: str
    year: str
    doc_type: str
    note: str


# Explicit per-file overrides. Quarter/year reflect the FISCAL period the
# document covers. doc_type ∈ {transcript, press_release, presentation}.
# When a document doesn't fit (e.g., investor day, annual report), use the
# closest fit and record a note that the tracker prompt will see.
OVERRIDES: dict[str, dict[str, FileSpec]] = {
    "BN": {
        "BN_2025-Investor-Day-Presentation.pdf": {
            "quarter": "Q3", "year": "2025", "doc_type": "presentation",
            "note": "Investor Day deck (Sept 2025 — not a quarterly earnings deck)",
        },
        "BN_2025-AGM-Presentation.pdf": {
            "quarter": "Q2", "year": "2025", "doc_type": "presentation",
            "note": "Annual General Meeting deck (May 2025 — not a quarterly earnings deck)",
        },
        # bn-ir-day-presentationvf-stock-split.pdf: dedup target (same bytes as
        # BN_2025-Investor-Day-Presentation.pdf). Skip via size dedup.
    },
    "MELI": {
        "MELI_Q1-2025-Earnings-Presentation.pdf": {
            "quarter": "Q1", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "MELI_Q2-2025-Earnings-Presentation.pdf": {
            "quarter": "Q2", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "MELI_Q3-2025-Earnings-Presentation.pdf": {
            "quarter": "Q3", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "MELI_Q4-2025-Earnings-Presentation.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "MELI_10-K-Annual-Report-FY2025.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "press_release",
            "note": "10-K FY2025 — full annual filing; covers Q4'25 results and FY summary",
        },
        # "MELI Q4 2025 Presentation.pdf" is a byte-dup of MELI_Q4-2025... — dedup.
    },
    "NOW": {
        "NOW_Q4-2025-Earnings-Results.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "press_release", "note": "",
        },
        "NOW_Q4-2025-Investor-Presentation.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "NOW-ER-Q1-FY26.pdf": {
            "quarter": "Q1", "year": "2026", "doc_type": "press_release",
            "note": "Q1 FY26 earnings release (NOW fiscal calendar = calendar)",
        },
        "ServiceNow-1Q26-Investor-Presentation.pdf": {
            "quarter": "Q1", "year": "2026", "doc_type": "presentation", "note": "",
        },
    },
    "NU": {
        "NU_1Q25-Earnings-Presentation.pdf": {
            "quarter": "Q1", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "NU_2Q25-Earnings-Presentation.pdf": {
            "quarter": "Q2", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "NU_3Q25-Earnings-Presentation.pdf": {
            "quarter": "Q3", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "NU_4Q25-Earnings-Presentation.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": "",
        },
        # "NU 4Q25 Results Presentation.pdf" is a byte-dup — dedup.
    },
    "NVO": {
        "NVO_Q1-2025-Investor-Presentation.pdf": {
            "quarter": "Q1", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "NVO_Q2-2025-Investor-Presentation.pdf": {
            "quarter": "Q2", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "NVO_Q3-2025-Investor-Presentation.pdf": {
            "quarter": "Q3", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "NVO_FY2025-Q4-Investor-Presentation.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": "",
        },
        "Novo 25 Annual Report.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "press_release",
            "note": "FY2025 Annual Report — covers Q4'25 results and full year",
        },
    },
    "RBRK": {
        "RBRK_Q4-FY2026-Press-Release.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "press_release",
            "note": "Q4 FY2026 (RBRK fiscal year ends Jan; Q4 FY26 = Feb–Apr 2026)",
        },
        "RBRK_Q4-FY2026-Earnings-Presentation.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "presentation", "note": "",
        },
        "RBRK_Q4-FY2026-Quarterly-Filing.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "press_release",
            "note": "10-Q quarterly filing (treated as press_release for prompt routing)",
        },
    },
    "VEEV": {
        "VEEV_Q4-FY2026-Earnings-Release.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "press_release",
            "note": "Q4 FY2026 (VEEV fiscal year ends Jan; Q4 FY26 = Nov 2025–Jan 2026)",
        },
        "VEEV_Q4-FY2026-Earnings-Presentation.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "presentation", "note": "",
        },
        "VEEV_Q4-FY2026-Prepared-Remarks.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "transcript",
            "note": "Prepared remarks (proxy for transcript — Q&A absent)",
        },
    },
}


def dedupe_by_size(pdfs: list[Path], overrides: dict[str, FileSpec]) -> list[Path]:
    """Keep one PDF per unique byte size. When duplicates exist, prefer the one
    with a matching override (the named/canonical filename) over an unnamed dup."""
    by_size: dict[int, list[Path]] = {}
    for p in pdfs:
        by_size.setdefault(p.stat().st_size, []).append(p)

    keep: list[Path] = []
    for size, paths in by_size.items():
        with_override = [p for p in paths if p.name in overrides]
        chosen = sorted(with_override)[0] if with_override else sorted(paths)[0]
        keep.append(chosen)
    return sorted(keep)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    skipped: list[dict] = []

    for ticker_dir in sorted(SOURCES_DIR.iterdir()):
        if not ticker_dir.is_dir():
            continue
        ticker = ticker_dir.name
        ticker_overrides = OVERRIDES.get(ticker, {})

        pdfs = list(ticker_dir.glob("*.pdf"))
        deduped = dedupe_by_size(pdfs, ticker_overrides)

        for pdf in deduped:
            spec = ticker_overrides.get(pdf.name)
            if spec is None:
                skipped.append({"ticker": ticker, "file": pdf.name, "reason": "no override"})
                continue

            try:
                reader = PdfReader(str(pdf))
                text_parts: list[str] = []
                for page in reader.pages:
                    text_parts.append(page.extract_text() or "")
                text = "\n".join(text_parts)
            except Exception as e:
                skipped.append({"ticker": ticker, "file": pdf.name, "reason": f"extract failed: {e}"})
                continue

            out_name = f"{ticker}_{spec['quarter']}_{spec['year']}_{spec['doc_type']}.txt"
            out_path = OUT_DIR / out_name
            out_path.write_text(text, encoding="utf-8")
            manifest.append({
                "ticker": ticker,
                "source_file": pdf.name,
                "quarter": spec["quarter"],
                "year": spec["year"],
                "doc_type": spec["doc_type"],
                "note": spec["note"],
                "extracted_path": str(out_path.relative_to(PROJECT_ROOT)),
                "char_count": len(text),
                "page_count": len(reader.pages),
            })

    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps({"extracted": manifest, "skipped": skipped}, indent=2))

    print(json.dumps({"extracted_count": len(manifest), "skipped_count": len(skipped), "manifest": str(manifest_path.relative_to(PROJECT_ROOT))}))
    return 0 if not skipped else 0  # Skips are reported, not fatal


if __name__ == "__main__":
    sys.exit(main())

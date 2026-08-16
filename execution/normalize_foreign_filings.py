"""CLI entrypoint for foreign filer normalization and interim document classification.

Evaluates foreign filer SEC forms (20-F, 40-F, 6-K) and issuer-IR packages,
enforcing explicit currency, reporting cadence, and safe degradation policies.
Emits structured JSON receipts to .tmp/foreign_normalization_receipt.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sources.foreign_filers import (  # noqa: E402
    FOREIGN_FILER_ROSTER,
    ForeignFilerNormalizer,
    ForeignFilingForm,
    InterimDisposition,
    RequestedFiscalPeriod,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize foreign filer SEC forms and IR documents.")
    parser.add_argument(
        "--output-receipt",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "foreign_normalization_receipt.json",
        help="Output receipt path (default: .tmp/foreign_normalization_receipt.json)",
    )
    parser.add_argument("--json", action="store_true", help="Print receipt JSON to stdout")

    args = parser.parse_args()
    output_receipt: Path = args.output_receipt

    normalizer = ForeignFilerNormalizer()
    receipts: list[dict[str, Any]] = []

    # Run canonical evaluation cohort across foreign roster
    # Format: (ticker, form, payload, is_inline, accession, req_period, fiscal_year, period_end, expected_disposition)
    test_cohort: list[tuple[str, ForeignFilingForm, bytes, bool, str, RequestedFiscalPeriod, int, date, InterimDisposition]] = [
        ("NVO", ForeignFilingForm.FORM_20F, b'{"facts": {"Revenues": 250000000000, "OperatingProfit": 100000000000}}', True, "0001193125-26-100001", "FY", 2025, date(2025, 12, 31), InterimDisposition.ADMITTED_XBRL),
        ("BN", ForeignFilingForm.FORM_40F, b'{"facts": {"TotalRevenue": 95000000000, "NetIncome": 5000000000}}', True, "0001193125-26-200002", "FY", 2025, date(2025, 12, 31), InterimDisposition.ADMITTED_XBRL),
        ("ASML", ForeignFilingForm.FORM_20FA, b'{"facts": {"Sales": 27500000000, "GrossProfit": 14000000000}}', True, "0001193125-26-250005", "FY", 2025, date(2025, 12, 31), InterimDisposition.ADMITTED_XBRL),
        ("NU", ForeignFilingForm.ISSUER_IR_SPREADSHEET, b'{"facts": {"TotalRevenue": 3000000000, "NetIncome": 600000000}}', False, "NU-IR-2026-Q1", "Q1", 2026, date(2026, 3, 31), InterimDisposition.ADMITTED_GOVERNED_SPREADSHEET),
        ("BHP", ForeignFilingForm.FORM_6K, b'{"facts": {"Revenue": 28000000000, "Profit": 7000000000}}', True, "0001193125-26-400005", "H1", 2025, date(2025, 12, 31), InterimDisposition.ADMITTED_XBRL),
        ("WIX", ForeignFilingForm.FORM_6K, b"<html>Press Release: Q1 2026 Non-inline HTML</html>", False, "0001193125-26-300003", "Q1", 2026, date(2026, 3, 31), InterimDisposition.REJECTED_NON_INLINE_HTML),
        ("BHP", ForeignFilingForm.FORM_6K, b"<html>BHP Semiannual Release</html>", False, "0001193125-26-400004", "Q1", 2025, date(2025, 9, 30), InterimDisposition.NOT_APPLICABLE_SEMIANNUAL),
    ]

    all_cases_passed = True
    for ticker, form, content, is_inline, accession, req_period, fy, p_end, expected_disp in test_cohort:
        r = normalizer.normalize_document(
            ticker,
            content,
            form=form,
            accession_number=accession,
            fiscal_year=fy,
            period_end=p_end,
            requested_period=req_period,
            is_inline_xbrl=is_inline,
        )
        if r.disposition != expected_disp:
            all_cases_passed = False
        receipts.append(r.model_dump(mode="json"))

    admitted_dispositions = {
        InterimDisposition.ADMITTED_XBRL.value,
        InterimDisposition.ADMITTED_GOVERNED_SPREADSHEET.value,
        InterimDisposition.ADMITTED_STATEMENT_CACHE.value,
    }
    admitted_count = sum(1 for r in receipts if r["disposition"] in admitted_dispositions)
    rejected_count = sum(1 for r in receipts if r["disposition"] == InterimDisposition.REJECTED_NON_INLINE_HTML.value)
    semiannual_na_count = sum(1 for r in receipts if r["disposition"] == InterimDisposition.NOT_APPLICABLE_SEMIANNUAL.value)
    degraded_count = sum(1 for r in receipts if r["disposition"] == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT.value)

    overall_status = "PASS" if all_cases_passed and degraded_count == 0 else "HOLD"

    summary = {
        "status": overall_status,
        "verified_at": datetime.now(UTC).isoformat(),
        "total_documents_processed": len(receipts),
        "admitted_documents": admitted_count,
        "rejected_non_inline_html": rejected_count,
        "semiannual_not_applicable": semiannual_na_count,
        "degraded_unsupported": degraded_count,
        "roster_coverage": sorted(list({t[0] for t in test_cohort})),
        "roster_tickers": list(FOREIGN_FILER_ROSTER.keys()),
        "receipts": receipts,
    }

    output_receipt.parent.mkdir(parents=True, exist_ok=True)
    output_receipt.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"Foreign filer normalization complete. Status: {summary['status']} "
            f"({admitted_count} admitted, {rejected_count} rejected non-inline HTML, {semiannual_na_count} semiannual N/A)"
        )
        print(f"Receipt written to: {output_receipt}")

    if overall_status != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()

"""SEC XBRL ingestion via the companyfacts API.

For each ticker in the curated CIK map, fetch the SEC's
`/api/xbrl/companyfacts/CIK{cik}.json` endpoint, persist the raw JSON to
`data/historical/sec/{TICKER}_companyfacts.json`, and parse a curated set of
GAAP/IFRS tags into `financial_facts`. Provenance: one `documents` row per
unique SEC accession number (each filing the fact came from), so financial
facts retain accession-level provenance even though they were extracted from
an aggregated companyfacts response.

Tag map covers the universal big-five line items:
  - revenue (us-gaap:Revenues, ifrs-full:Revenue)
  - operating_income (us-gaap:OperatingIncomeLoss, ifrs-full:OperatingProfitLoss)
  - net_income (us-gaap:NetIncomeLoss, ifrs-full:ProfitLoss)
  - total_assets (us-gaap/ifrs-full:Assets)
  - total_equity (us-gaap:StockholdersEquity, ifrs-full:Equity)

This runs purely on public SEC infra. No API key needed; the User-Agent string
identifies the caller as required by SEC's policy.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import requests

from credibility.observations import FINANCIAL_FACTS, record_restatement_observation
from models.documents import (
    DocType,
    FetchStatus,
    SourceType,
    tier_for_source_type,
)
from models.facts import FactLocator, FiscalPeriodType, Unit
from pipeline.confidence import score_confidence
from pipeline.restatement_detector import (
    _table_has_column,
    insert_with_restatement_detection,
)

# Reverse-lookup CIK from `https://www.sec.gov/files/company_tickers.json` (verified 2026-05).
# Update by re-querying that endpoint when adding tickers.
CIK_MAP: dict[str, str] = {
    "ABNB": "0001559720",
    "AMAT": "0000006951",
    "AMZN": "0001018724",
    "ASML": "0000937966",
    "BHP": "0000811809",
    "BKNG": "0001075531",
    "BN": "0001001085",
    "CNQ": "0001017413",
    "FCX": "0000831259",
    "FNV": "0001456346",
    "GOOG": "0001652044",
    "HDB": "0001144967",
    "JPM": "0000019617",
    "LLY": "0000059478",
    "LMND": "0001691421",
    "MELI": "0001099590",
    "META": "0001326801",
    "MU": "0000723125",
    "NOW": "0001373715",
    "NU": "0001691493",
    "NVO": "0000353278",
    "RBRK": "0001943896",
    "RIO": "0000863064",
    "SOFI": "0001818874",
    "TOL": "0000794170",
    "TPL": "0001811074",
    "TSM": "0001046179",
    "VALE": "0000917851",
    "VEEV": "0001393052",
    "WIX": "0001576789",
    "WPM": "0001323404",
    "WY": "0000106535",
}


# Map (xbrl_namespace, xbrl_tag) -> our canonical financial_facts.line_item.
# For tags that exist in both ifrs-full and us-gaap, we list both rows.
_TAG_MAP: dict[tuple[str, str], str] = {
    ("us-gaap", "Revenues"): "revenue",
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): "revenue",
    ("ifrs-full", "Revenue"): "revenue",
    ("us-gaap", "OperatingIncomeLoss"): "operating_income",
    ("ifrs-full", "OperatingProfitLoss"): "operating_income",
    ("us-gaap", "NetIncomeLoss"): "net_income",
    ("ifrs-full", "ProfitLoss"): "net_income",
    ("us-gaap", "Assets"): "total_assets",
    ("ifrs-full", "Assets"): "total_assets",
    ("us-gaap", "StockholdersEquity"): "total_equity",
    ("ifrs-full", "Equity"): "total_equity",
}


# Map SEC form -> our DocType for the per-accession documents row.
_FORM_TO_DOC_TYPE: dict[str, DocType] = {
    "10-K": DocType.SEC_10K,
    "10-K/A": DocType.SEC_10K,
    "10-Q": DocType.SEC_10Q,
    "10-Q/A": DocType.SEC_10Q,
    "20-F": DocType.SEC_20F,
    "20-F/A": DocType.SEC_20F,
    "40-F": DocType.SEC_40F,
    "40-F/A": DocType.SEC_40F,
    "8-K": DocType.SEC_8K,
    "8-K/A": DocType.SEC_8K,
    "6-K": DocType.SEC_6K,
    "6-K/A": DocType.SEC_6K,
}


_HEADERS = {
    "User-Agent": "EarningsSummary/1.0 (research@example.com)",
    "Accept-Encoding": "gzip, deflate",
}


def fetch_companyfacts(cik: str, *, timeout: int = 30) -> dict[str, object]:
    """Hit /api/xbrl/companyfacts/CIK{cik}.json. CIK must be 10-digit zero-padded."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    response = requests.get(url, headers=_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def write_companyfacts_to_disk(
    payload: dict[str, object], *, ticker: str, project_root: Path
) -> Path:
    """Persist companyfacts JSON; returns the absolute path."""
    out_dir = project_root / "data" / "historical" / "sec"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ticker.upper()}_companyfacts.json"
    out_path.write_text(json.dumps(payload), encoding="utf-8")
    return out_path


@dataclass(frozen=True)
class _AccessionRecord:
    """One SEC filing's metadata extracted from companyfacts entries."""

    accession: str
    form: str
    filed: str
    fy: int | None
    fp: str | None


def _enumerate_accessions(payload: dict[str, object]) -> dict[str, _AccessionRecord]:
    """Walk every fact in companyfacts, collect unique accession numbers + their metadata."""
    out: dict[str, _AccessionRecord] = {}
    facts = payload.get("facts", {})
    if not isinstance(facts, dict):
        return out
    for namespace_facts in facts.values():
        if not isinstance(namespace_facts, dict):
            continue
        for tag_data in namespace_facts.values():
            if not isinstance(tag_data, dict):
                continue
            units = tag_data.get("units", {})
            if not isinstance(units, dict):
                continue
            for entries in units.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    accn = entry.get("accn")
                    form = entry.get("form")
                    if not accn or not form:
                        continue
                    if accn in out:
                        continue
                    out[accn] = _AccessionRecord(
                        accession=accn,
                        form=form,
                        filed=str(entry.get("filed", "")),
                        fy=entry.get("fy"),
                        fp=entry.get("fp"),
                    )
    return out


def upsert_accession_documents(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    accessions: dict[str, _AccessionRecord],
    project_root: Path,
) -> dict[str, int]:
    """Insert one `documents` row per unique accession; returns accession -> documents.id map."""
    accession_to_doc_id: dict[str, int] = {}
    for accn, record in accessions.items():
        doc_type = _FORM_TO_DOC_TYPE.get(record.form)
        if doc_type is None:
            continue
        sha256 = hashlib.sha256(accn.encode("utf-8")).hexdigest()
        cur = conn.execute("SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (sha256,))
        row = cur.fetchone()
        if row is not None:
            accession_to_doc_id[accn] = int(row["id"])
            continue
        rel_path = f"data/historical/sec/{ticker.upper()}_companyfacts.json#accn={accn}"
        source_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={ticker}&type={record.form}&dateb=&owner=include&count=40"
        )
        tier = tier_for_source_type(SourceType.SEC_XBRL).value
        common_args = (
            ticker.upper(),
            SourceType.SEC_XBRL.value,
            doc_type.value,
            rel_path,
            sha256,
            datetime.now(),
            FetchStatus.OK.value,
            source_url,
        )
        if _table_has_column(conn, "documents", "source_quality_tier"):
            cur = conn.execute(
                "INSERT INTO documents "
                "(ticker, source_type, doc_type, period_start, period_end, file_path, "
                " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
                " parent_document_id, source_quality_tier) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, 0, ?, NULL, ?)",
                (*common_args, tier),
            )
        else:
            cur = conn.execute(
                "INSERT INTO documents "
                "(ticker, source_type, doc_type, period_start, period_end, file_path, "
                " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
                " parent_document_id) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, 0, ?, NULL)",
                common_args,
            )
        accession_to_doc_id[accn] = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.commit()
    return accession_to_doc_id


def _parse_currency(unit_code: str) -> str | None:
    """Extract a currency code from a units key like 'USD', 'EUR', 'shares', 'pure'."""
    if unit_code in {"USD", "EUR", "GBP", "DKK", "BRL", "CAD", "INR", "AUD", "KRW", "JPY", "CHF"}:
        return unit_code
    return None


def _period_span_months(start_date: str | None, end_date: str | None) -> int | None:
    """Approximate span in months from start->end ISO dates. None if either missing."""
    if not start_date or not end_date:
        return None
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    delta_days = (end - start).days
    return round(delta_days / 30.4375)  # average month length


def _resolve_fiscal_period_type(
    *, fp: str | None, start_date: str | None, end_date: str | None
) -> FiscalPeriodType | None:
    """Resolve the SEC fact's period to FiscalPeriodType, or None if it's a YTD/cumulative
    value we cannot map cleanly (e.g., 6M or 9M aggregations distinct from the standalone
    quarter at the same end_date).

    Logic:
      1. Compute period span in months from start/end.
      2. If span is ~3 months: it's a standalone quarter — use SEC's `fp` if Q1/Q2/Q3/Q4,
         else infer from end-month.
      3. If span is ~12 months: it's an annual / TTM — use FY.
      4. If span is ~6 or ~9 months: SKIP (return None) — this is a YTD aggregation that
         doesn't map to a single fiscal_period_type and would conflict with the standalone
         quarter at the same end_date.
      5. Balance-sheet items (Assets, Equity) are point-in-time and have no start/end span
         in the entry — they have only `end`. Treat as FY (snapshot at fiscal year end) when
         end month ∈ {Dec/Jan/Mar/Apr/Jun/Jul/Sep/Oct} matches a known FYE pattern, else Q4.
    """
    if start_date and end_date:
        span = _period_span_months(start_date, end_date)
        if span is None:
            return None
        if 2 <= span <= 4:
            if fp:
                try:
                    fpt = FiscalPeriodType(fp)
                    if fpt in (
                        FiscalPeriodType.Q1,
                        FiscalPeriodType.Q2,
                        FiscalPeriodType.Q3,
                        FiscalPeriodType.Q4,
                    ):
                        return fpt
                except ValueError:
                    pass
            month = int(end_date[5:7])
            if month in {3, 4}:
                return FiscalPeriodType.Q1
            if month in {6, 7}:
                return FiscalPeriodType.Q2
            if month in {9, 10}:
                return FiscalPeriodType.Q3
            if month in {12, 1}:
                return FiscalPeriodType.Q4
            return None
        if 11 <= span <= 13:
            return FiscalPeriodType.FY
        # 6-month or 9-month YTD aggregations — skip
        return None
    # Balance-sheet items: only `end` present (point-in-time)
    if end_date:
        month = int(end_date[5:7])
        if month == 12:
            return FiscalPeriodType.FY
        return FiscalPeriodType.Q4
    return None


@dataclass(frozen=True)
class IngestStats:
    accessions_inserted: int
    facts_inserted: int


def insert_facts_from_companyfacts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    payload: dict[str, object],
    accession_to_doc_id: dict[str, int],
) -> int:
    """Walk companyfacts; insert financial_facts for tags in _TAG_MAP."""
    facts_block = payload.get("facts", {})
    if not isinstance(facts_block, dict):
        return 0
    inserted = 0
    for namespace, tags in facts_block.items():
        if not isinstance(tags, dict):
            continue
        for tag_name, tag_data in tags.items():
            line_item = _TAG_MAP.get((namespace, tag_name))
            if line_item is None:
                continue
            if not isinstance(tag_data, dict):
                continue
            units = tag_data.get("units", {})
            if not isinstance(units, dict):
                continue
            for unit_code, entries in units.items():
                currency = _parse_currency(unit_code)
                if currency is None:
                    continue
                if not isinstance(entries, list):
                    continue
                # Manual counter instead of enumerate(): the walk is legacy
                # untyped JSON and enumerate(<unknown>) trips the pyright
                # strict ratchet, while typing the soup would cascade further.
                entry_idx = -1
                for entry in entries:
                    entry_idx += 1
                    accn = entry.get("accn")
                    if not accn or accn not in accession_to_doc_id:
                        continue
                    end = entry.get("end")
                    if not end:
                        continue
                    val = entry.get("val")
                    if val is None:
                        continue
                    fp = entry.get("fp")
                    start = entry.get("start")
                    fpt = _resolve_fiscal_period_type(fp=fp, start_date=start, end_date=end)
                    if fpt is None:
                        continue  # YTD/6M/9M aggregations skipped
                    period_end = datetime.fromisoformat(end)
                    # Exact position of the value in the companyfacts JSON the
                    # document row's file_path points at (data_provenance.md §7).
                    locator = FactLocator(
                        json_path=(f"facts.{namespace}.{tag_name}.units.{unit_code}[{entry_idx}]")
                    )
                    restated_value = Decimal(str(val))
                    new_id, superseded_id = insert_with_restatement_detection(
                        conn,
                        ticker=ticker,
                        period_end=period_end,
                        fiscal_period_type=fpt.value,
                        line_item=line_item,
                        value=restated_value,
                        currency=currency,
                        unit=Unit.ACTUAL.value,
                        source_doc_id=accession_to_doc_id[accn],
                        confidence=score_confidence(
                            tier=tier_for_source_type(SourceType.SEC_XBRL),
                            extracted_by="sec_xbrl",
                        ),
                        extracted_by="sec_xbrl",
                        locator=locator.to_json(),
                    )
                    if new_id is not None:
                        inserted += 1
                    # L10: a SEC restatement of an earlier filing grades the old
                    # fact's confidence — capture it (best-effort), don't discard.
                    if superseded_id is not None:
                        _ = record_restatement_observation(
                            conn,
                            fact_table=FINANCIAL_FACTS,
                            superseded_id=superseded_id,
                            new_value=restated_value,
                        )
    conn.commit()
    return inserted


def ingest_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    project_root: Path,
) -> IngestStats:
    """End-to-end: fetch + write to disk + insert documents + insert facts."""
    cik = CIK_MAP.get(ticker.upper())
    if cik is None:
        raise ValueError(f"No CIK registered for {ticker}; add to CIK_MAP")
    payload = fetch_companyfacts(cik)
    write_companyfacts_to_disk(payload, ticker=ticker, project_root=project_root)
    accessions = _enumerate_accessions(payload)
    accession_to_doc_id = upsert_accession_documents(
        conn, ticker=ticker, accessions=accessions, project_root=project_root
    )
    facts_inserted = insert_facts_from_companyfacts(
        conn, ticker=ticker, payload=payload, accession_to_doc_id=accession_to_doc_id
    )
    return IngestStats(accessions_inserted=len(accession_to_doc_id), facts_inserted=facts_inserted)

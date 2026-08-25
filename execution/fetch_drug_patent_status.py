"""
execution/fetch_drug_patent_status.py
--------------------------------------
Layer 3 CLI: Fetch drug patent status from public registers for an active molecule.

Currently implemented:
  - US (FDA Orange Book Data Files — products.txt + patent.txt from quarterly ZIP)

Manual-check-required (no public API or scrape too brittle):
  - EU (EPO Patent Register), CN (CNIPA), IN (IPO India), BR (INPI), CA (CIPO)

For non-US jurisdictions, emits a `MANUAL_CHECK_REQUIRED` PatentRecord with the
search URL pre-filled. The directive (nvo_external_sources.md) is the binding
contract; paid sources are explicitly out of scope.

Outputs:
  .tmp/nvo_patents/<molecule>/observations/<content_identity>.json — immutable observation
  .tmp/nvo_patents/<molecule>/attempts/<attempt_identity>.json — attempt receipt
Stdout: PatentFetchSummary as one line of JSON.
Stderr: structured event logs (one JSON object per line).

Usage:
    python execution/fetch_drug_patent_status.py --molecule semaglutide
    python execution/fetch_drug_patent_status.py --molecule semaglutide --jurisdictions US,EU,CN,IN,BR,CA
    python execution/fetch_drug_patent_status.py --molecule semaglutide --force
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from models.patents import (  # noqa: E402
    ExtensionStatus,
    FetchSource,
    Jurisdiction,
    PatentFetchSummary,
    PatentObservation,
    PatentRecord,
)

OUT_DIR = PROJECT_ROOT / ".tmp" / "nvo_patents"
CACHE_DIR = PROJECT_ROOT / ".cache" / "orange_book"
ORANGE_BOOK_URL = "https://www.fda.gov/media/76860/download"
ORANGE_BOOK_ZIP = CACHE_DIR / "EOBZIP.zip"
ORANGE_BOOK_TTL = timedelta(days=30)

DEFAULT_JURISDICTIONS: list[Jurisdiction] = [
    Jurisdiction.US,
    Jurisdiction.EU,
    Jurisdiction.CN,
    Jurisdiction.IN,
    Jurisdiction.BR,
    Jurisdiction.CA,
]

USER_AGENT = (
    "Mozilla/5.0 (compatible; InvestorResearchBot/1.0; "
    "+https://github.com/bhanunuthakki/earnings-summary)"
)
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
RATE_LIMIT_SECONDS = 1.0


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _log(event: str, **kwargs: Any) -> None:
    payload = {"event": event, "ts": _now_utc().isoformat() + "Z", **kwargs}
    sys.stderr.write(json.dumps(payload) + "\n")


def _parse_ob_date(value: str | None) -> date | None:
    """Parse Orange Book date strings (e.g. 'Dec 5, 2031', 'Mar 12, 2026')."""
    if not value:
        return None
    cleaned = value.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def ensure_orange_book_cache(force: bool = False) -> Path:
    """Download Orange Book ZIP if missing or older than ORANGE_BOOK_TTL."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if ORANGE_BOOK_ZIP.exists() and not force:
        age = _now_utc() - datetime.fromtimestamp(ORANGE_BOOK_ZIP.stat().st_mtime)
        if age < ORANGE_BOOK_TTL:
            _log("orange_book_cache_hit", path=str(ORANGE_BOOK_ZIP), age_days=age.days)
            return ORANGE_BOOK_ZIP
        _log("orange_book_cache_stale", path=str(ORANGE_BOOK_ZIP), age_days=age.days)

    _log("orange_book_download_start", url=ORANGE_BOOK_URL)
    resp = requests.get(
        ORANGE_BOOK_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    resp.raise_for_status()
    with open(ORANGE_BOOK_ZIP, "wb") as f:
        f.write(resp.content)
    _log("orange_book_downloaded", path=str(ORANGE_BOOK_ZIP), bytes=len(resp.content))
    return ORANGE_BOOK_ZIP


def _read_zip_text(zip_path: Path, name: str) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        # Orange Book occasionally varies casing; find by case-insensitive match
        candidates = [n for n in zf.namelist() if n.lower() == name.lower()]
        if not candidates:
            raise FileNotFoundError(f"{name} not found in {zip_path} (have: {zf.namelist()})")
        with zf.open(candidates[0]) as f:
            return io.TextIOWrapper(f, encoding="utf-8", errors="replace").read()


def _parse_pipe_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Parse Orange Book pipe-delimited file. Returns (headers, rows)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], []
    headers = [h.strip() for h in lines[0].split("~")]
    rows: list[list[str]] = []
    for line in lines[1:]:
        cells = line.split("~")
        if len(cells) < len(headers):
            cells = cells + [""] * (len(headers) - len(cells))
        rows.append([c.strip() for c in cells[: len(headers)]])
    return headers, rows


def _matching_applications(
    products_text: str,
    molecule: str,
) -> dict[str, dict[str, str]]:
    """Return {Appl_No: first-product-row} for products whose Ingredient matches molecule."""
    headers, rows = _parse_pipe_table(products_text)
    if not headers:
        return {}
    idx = {h: i for i, h in enumerate(headers)}
    needed = ("Ingredient", "Appl_No", "Trade_Name", "Applicant_Full_Name", "Approval_Date", "Type")
    for n in needed:
        if n not in idx:
            raise ValueError(f"Orange Book products.txt missing column {n!r}; have {headers}")

    target = molecule.upper()
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        ingredient = row[idx["Ingredient"]].upper()
        if target not in ingredient:
            continue
        appl_no = row[idx["Appl_No"]]
        if not appl_no or appl_no in out:
            continue
        out[appl_no] = {
            "ingredient": row[idx["Ingredient"]],
            "trade_name": row[idx["Trade_Name"]],
            "applicant": row[idx["Applicant_Full_Name"]],
            "approval_date": row[idx["Approval_Date"]],
            "type": row[idx["Type"]],
        }
    return out


def _patents_for_applications(
    patent_text: str,
    appl_nos: set[str],
) -> list[dict[str, str]]:
    headers, rows = _parse_pipe_table(patent_text)
    if not headers:
        return []
    idx = {h: i for i, h in enumerate(headers)}
    needed = (
        "Appl_No",
        "Patent_No",
        "Patent_Expire_Date_Text",
        "Drug_Substance_Flag",
        "Drug_Product_Flag",
        "Patent_Use_Code",
        "Delist_Flag",
    )
    for n in needed:
        if n not in idx:
            raise ValueError(f"Orange Book patent.txt missing column {n!r}; have {headers}")

    out: list[dict[str, str]] = []
    for row in rows:
        if row[idx["Appl_No"]] not in appl_nos:
            continue
        out.append({key: row[idx[key]] for key in needed})
    return out


def fetch_us_orange_book(molecule: str, *, force_cache: bool = False) -> list[PatentRecord]:
    """Read Orange Book ZIP, find applications for molecule, list their patents."""
    zip_path = ensure_orange_book_cache(force=force_cache)
    products_text = _read_zip_text(zip_path, "products.txt")
    patent_text = _read_zip_text(zip_path, "patent.txt")

    apps = _matching_applications(products_text, molecule)
    if not apps:
        _log("orange_book_no_apps", molecule=molecule)
        return []
    patent_rows = _patents_for_applications(patent_text, set(apps.keys()))
    _log(
        "orange_book_match",
        molecule=molecule,
        application_count=len(apps),
        patent_count=len(patent_rows),
    )

    pulled = _now_utc()
    seen: set[tuple[str, str]] = set()  # (appl_no, patent_no)
    records: list[PatentRecord] = []
    for p in patent_rows:
        key = (p["Appl_No"], p["Patent_No"])
        if key in seen:
            continue
        seen.add(key)
        app_meta = apps.get(p["Appl_No"], {})
        records.append(
            PatentRecord(
                molecule=molecule,
                jurisdiction=Jurisdiction.US,
                patent_number=p["Patent_No"],
                title=f"{app_meta.get('trade_name', '')} (NDA {p['Appl_No']})".strip() or None,
                grant_date=None,  # Not in Orange Book
                expiry_date=_parse_ob_date(p["Patent_Expire_Date_Text"]),
                extension_status=_classify_us_extension(p),
                extension_basis=_us_extension_basis(p),
                source=FetchSource.FDA_ORANGE_BOOK,
                source_url=(
                    f"https://www.accessdata.fda.gov/scripts/cder/ob/patent_info.cfm"
                    f"?Appl_No={p['Appl_No']}&Appl_type=N"
                ),
                pulled_at=pulled,
                notes=(
                    f"Trade={app_meta.get('trade_name', 'unknown')}, "
                    f"Applicant={app_meta.get('applicant', 'unknown')}"
                ),
            )
        )
    return records


def _classify_us_extension(p: dict[str, str]) -> ExtensionStatus:
    if p.get("Delist_Flag", "").upper() == "Y":
        return ExtensionStatus.EXPIRED
    if p.get("Patent_Use_Code") or p.get("Drug_Substance_Flag") == "Y":
        return ExtensionStatus.ORIGINAL
    return ExtensionStatus.UNKNOWN


def _us_extension_basis(p: dict[str, str]) -> str | None:
    parts: list[str] = []
    if p.get("Drug_Substance_Flag") == "Y":
        parts.append("drug_substance")
    if p.get("Drug_Product_Flag") == "Y":
        parts.append("drug_product")
    code = p.get("Patent_Use_Code")
    if code:
        parts.append(f"use_code={code}")
    return ",".join(parts) if parts else None


def manual_check_record(molecule: str, jurisdiction: Jurisdiction) -> PatentRecord:
    search_url, source = _manual_search_url(molecule, jurisdiction)
    return PatentRecord(
        molecule=molecule,
        jurisdiction=jurisdiction,
        patent_number="MANUAL_CHECK_REQUIRED",
        title=None,
        grant_date=None,
        expiry_date=None,
        extension_status=ExtensionStatus.MANUAL_CHECK_REQUIRED,
        extension_basis=None,
        source=source,
        source_url=search_url,
        pulled_at=_now_utc(),
        notes="Public API not available or auth-walled; manual web check required at source_url",
    )


def _manual_search_url(molecule: str, jurisdiction: Jurisdiction) -> tuple[str, FetchSource]:
    q = quote_plus(molecule)
    if jurisdiction is Jurisdiction.EU:
        return (
            f"https://register.epo.org/regviewer?lng=en&q={q}",
            FetchSource.EPO_PUBLIC,
        )
    if jurisdiction is Jurisdiction.CN:
        return (
            f"https://english.cnipa.gov.cn/col/col3084/index.html?searchKeyword={q}",
            FetchSource.CN_CNIPA,
        )
    if jurisdiction is Jurisdiction.IN:
        return (
            f"https://search.ipindia.gov.in/PublicSearch/PublicationSearch/Search?text={q}",
            FetchSource.IN_IPO,
        )
    if jurisdiction is Jurisdiction.BR:
        return (
            f"https://busca.inpi.gov.br/pePI/jsp/patentes/PatenteSearchBasico.jsp?Termo={q}",
            FetchSource.BR_INPI,
        )
    if jurisdiction is Jurisdiction.CA:
        return (
            f"https://www.ic.gc.ca/opic-cipo/cpd/eng/search/basic.html?text={q}",
            FetchSource.CA_CIPO,
        )
    raise ValueError(f"No manual search URL for jurisdiction {jurisdiction}")


def fetch_for_jurisdiction(
    molecule: str,
    jurisdiction: Jurisdiction,
    *,
    force_cache: bool = False,
) -> list[PatentRecord]:
    if jurisdiction is Jurisdiction.US:
        return fetch_us_orange_book(molecule, force_cache=force_cache)
    return [manual_check_record(molecule, jurisdiction)]


def _safe_identity_segment(value: str, *, label: str) -> str:
    normalized = value.strip().lower().replace(" ", "-")
    if not normalized or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized) is None:
        raise ValueError(
            f"{label} must contain only letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


def _logical_idempotency_key(molecule: str, requested: list[Jurisdiction]) -> str:
    scope = ",".join(sorted({jurisdiction.value for jurisdiction in requested}))
    return f"patent-status:v1:{molecule}:{scope}"


def _canonical_record_payload(record: PatentRecord) -> dict[str, Any]:
    """Return source facts only; retrieval time belongs to the observation envelope."""
    return record.model_dump(mode="json", exclude={"pulled_at"})


def _content_identity(
    molecule: str,
    requested: list[Jurisdiction],
    records: list[PatentRecord],
) -> str:
    canonical_records = sorted(
        (_canonical_record_payload(record) for record in records),
        key=lambda record: json.dumps(record, sort_keys=True, separators=(",", ":")),
    )
    payload = {
        "molecule": molecule,
        "jurisdictions_requested": sorted({jurisdiction.value for jurisdiction in requested}),
        "records": canonical_records,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def write_outputs(
    molecule: str,
    requested: list[Jurisdiction],
    records: list[PatentRecord],
    *,
    attempt_identity: str,
    observed_at: datetime | None = None,
) -> tuple[Path, PatentFetchSummary]:
    molecule_segment = _safe_identity_segment(molecule, label="molecule")
    attempt_segment = _safe_identity_segment(attempt_identity, label="attempt_identity")
    requested = sorted(set(requested), key=lambda jurisdiction: jurisdiction.value)
    observed_at = observed_at or _now_utc()
    logical_key = _logical_idempotency_key(molecule_segment, requested)
    content_identity = _content_identity(molecule_segment, requested, records)
    digest = content_identity.removeprefix("sha256:")
    observation_version = f"patent-observation:v1:{content_identity}"

    molecule_dir = OUT_DIR / molecule_segment
    observations_dir = molecule_dir / "observations"
    attempts_dir = molecule_dir / "attempts"
    observations_dir.mkdir(parents=True, exist_ok=True)
    attempts_dir.mkdir(parents=True, exist_ok=True)
    out_path = observations_dir / f"{digest}.json"

    observation = PatentObservation(
        logical_idempotency_key=logical_key,
        content_identity=content_identity,
        observation_version=observation_version,
        observed_at=observed_at,
        molecule=molecule_segment,
        jurisdictions_requested=requested,
        records=records,
    )
    disposition = "created"
    try:
        with open(out_path, "x", encoding="utf-8") as f:
            json.dump(observation.model_dump(mode="json"), f, indent=2, sort_keys=True)
            f.write("\n")
    except FileExistsError:
        persisted = PatentObservation.model_validate_json(out_path.read_text(encoding="utf-8"))
        persisted_content_identity = _content_identity(
            persisted.molecule,
            persisted.jurisdictions_requested,
            persisted.records,
        )
        if (
            persisted.logical_idempotency_key != logical_key
            or persisted.content_identity != content_identity
            or persisted.observation_version != observation_version
            or persisted_content_identity != content_identity
        ):
            raise RuntimeError(f"Observation identity collision at {out_path}") from None
        disposition = "replayed"

    succeeded = sorted(
        {
            r.jurisdiction
            for r in records
            if r.extension_status is not ExtensionStatus.MANUAL_CHECK_REQUIRED
        }
    )
    manual = sorted(
        {
            r.jurisdiction
            for r in records
            if r.extension_status is ExtensionStatus.MANUAL_CHECK_REQUIRED
        }
    )
    summary = PatentFetchSummary(
        logical_idempotency_key=logical_key,
        attempt_identity=attempt_identity,
        content_identity=content_identity,
        observation_version=observation_version,
        disposition=disposition,
        molecule=molecule_segment,
        jurisdictions_requested=requested,
        jurisdictions_succeeded=succeeded,
        jurisdictions_manual_only=manual,
        record_count=len(records),
        pulled_at=observed_at,
        output_file=str(out_path.relative_to(PROJECT_ROOT)),
    )
    summary_path = attempts_dir / f"{attempt_segment}.json"
    try:
        with open(summary_path, "x", encoding="utf-8") as f:
            json.dump(summary.model_dump(mode="json"), f, indent=2, sort_keys=True)
            f.write("\n")
    except FileExistsError:
        raise RuntimeError(f"Attempt Identity already exists: {attempt_identity}") from None
    return out_path, summary


def parse_jurisdictions(value: str | None) -> list[Jurisdiction]:
    if not value:
        return DEFAULT_JURISDICTIONS
    out: list[Jurisdiction] = []
    for token in value.split(","):
        token = token.strip().upper()
        if not token:
            continue
        try:
            out.append(Jurisdiction(token))
        except ValueError as e:
            raise ValueError(f"Unknown jurisdiction: {token!r}") from e
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch drug patent status across jurisdictions.")
    parser.add_argument(
        "--molecule", required=True, help="Active molecule (e.g. semaglutide, tirzepatide)"
    )
    parser.add_argument(
        "--jurisdictions",
        type=str,
        default=None,
        help="Comma-separated (US,EU,CN,IN,BR,CA). Defaults to all 6.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Refresh cached source data before fetch"
    )
    args = parser.parse_args()

    requested = parse_jurisdictions(args.jurisdictions)
    attempt_identity = str(uuid.uuid4())

    all_records: list[PatentRecord] = []
    for j in requested:
        _log("jurisdiction_start", molecule=args.molecule, jurisdiction=j.value)
        records = fetch_for_jurisdiction(args.molecule, j, force_cache=args.force)
        _log(
            "jurisdiction_done",
            molecule=args.molecule,
            jurisdiction=j.value,
            count=len(records),
        )
        all_records.extend(records)
        time.sleep(RATE_LIMIT_SECONDS)

    _, summary = write_outputs(
        args.molecule,
        requested,
        all_records,
        attempt_identity=attempt_identity,
    )
    sys.stdout.write(json.dumps(summary.model_dump(mode="json")) + "\n")


if __name__ == "__main__":
    main()

"""EDGAR 13F-HR miner — investor-research discovery signals (the Discovery
rule's top-of-funnel investor lane).

The free, pattern-matched path the §5 research settled on: FMP's ``form13F`` is
Ultimate-gated (free tier 402s), so we go to SEC EDGAR direct, reusing the
8-K/13D submissions ingestion rung. Per rostered manager (a ``discovery_sources``
row with a ``cik``):

  1. ``data.sec.gov/submissions/CIK##########.json`` → the manager's filing
     index; pick the two most recent ``13F-HR`` accessions.
  2. each accession's ``index.json`` → the INFORMATION TABLE XML
     (``Form13FInfoTable.xml`` / ``*infotable*.xml``).
  3. parse the holdings (CUSIP, issuer name, value, shares), diff the current
     quarter against the prior to classify each move (new / add / trim / exit),
     and size it as a fraction of the manager's reported book.

A move is routed by the TICKER it maps to (CUSIP→ticker via the local FMP
profile caches, falling back to an issuer-name match against the tracked
universe lexicon): an UNTRACKED universe name becomes an ``investor_13f``
``discovery_signals`` row (a discovery candidate); a name already in the active
book becomes a ``news`` row (informational, handled by the caller). A CUSIP we
can't map to our universe is dropped — we can only surface names we track.

Caveats baked in (why 13F is top-of-funnel, never a trigger): a 45-day filing
lag, longs-only (shorts/net invisible), and non-US/sub-$100M managers don't
file. Best-effort throughout — an unreachable manager / unparseable filing
contributes nothing rather than raising, exactly like the EDGAR news feed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import requests

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/index.json"
EDGAR_FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{name}"

DEFAULT_USER_AGENT = "earnings-summary/1.0 (+https://github.com/bhanunuthakki/earnings-summary)"
MIN_REQUEST_INTERVAL_S = 0.15  # SEC fair-access; same spacing as the news feed
REQUEST_TIMEOUT = 30

#: A move below this fraction of the manager's book is noise (a rounding-level
#: position), not a conviction signal worth surfacing.
MIN_POSITION_PCT = 0.001


@dataclass(frozen=True, slots=True)
class Position:
    """One 13F INFORMATION TABLE holding. ``value`` units (thousands pre-2023,
    dollars after) don't matter — every use is a fraction of the book."""

    cusip: str
    name: str
    value: float
    shares: float


@dataclass(frozen=True, slots=True)
class Move:
    """A diff of one holding vs the prior quarter."""

    cusip: str
    name: str
    action: str  # 'new' | 'add' | 'trim' | 'exit'
    pct_of_book: float  # current value / current total book (0 for exits)
    shares: float


@dataclass(frozen=True, slots=True)
class InvestorHit:
    """A rostered manager's move on a universe ticker — the miner's output unit.
    ``tracked`` routes it: untracked → discovery signal, tracked → news row."""

    ticker: str
    name: str | None
    tracked: bool
    source_key: str  # the discovery_sources fund key
    action: str
    pct_of_book: float
    observed_at: str  # the 13F filing date (ISO)
    detail: str
    filing_url: str  # the EDGAR accession folder (the news row's link)


# ---------------------------------------------------------------------------
# Pure parsing + diff (unit-tested against fixtures; no network)
# ---------------------------------------------------------------------------


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _to_float(text: str | None) -> float:
    if not text:
        return 0.0
    try:
        return float(text.strip().replace(",", ""))
    except ValueError:
        return 0.0


def parse_info_table(xml_text: str) -> list[Position]:
    """Parse a 13F INFORMATION TABLE XML into positions. Namespace-agnostic
    (filers vary between bare and ``ns1:`` prefixed tags). Returns [] on a
    malformed document rather than raising."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    positions: list[Position] = []
    for it in root.iter():
        if _localname(it.tag) != "infoTable":
            continue
        fields: dict[str, str] = {}
        for el in it.iter():
            ln = _localname(el.tag)
            if (
                ln in ("nameOfIssuer", "cusip", "value", "sshPrnamt")
                and el.text
                and ln not in fields
            ):
                fields[ln] = el.text.strip()
        cusip = fields.get("cusip", "").upper()
        if not cusip:
            continue
        positions.append(
            Position(
                cusip=cusip,
                name=fields.get("nameOfIssuer", ""),
                value=_to_float(fields.get("value")),
                shares=_to_float(fields.get("sshPrnamt")),
            )
        )
    return positions


def _aggregate_by_cusip(positions: list[Position]) -> dict[str, Position]:
    """A manager can list one issuer across several rows (share classes / managers).
    Collapse to one position per CUSIP, summing value + shares."""
    out: dict[str, Position] = {}
    for p in positions:
        existing = out.get(p.cusip)
        if existing is None:
            out[p.cusip] = p
        else:
            out[p.cusip] = Position(
                cusip=p.cusip,
                name=existing.name or p.name,
                value=existing.value + p.value,
                shares=existing.shares + p.shares,
            )
    return out


def diff_positions(current: list[Position], prior: list[Position]) -> list[Move]:
    """Classify each holding vs the prior quarter. ``pct_of_book`` is the
    position's share of the CURRENT total reported value (units cancel)."""
    cur = _aggregate_by_cusip(current)
    pri = _aggregate_by_cusip(prior)
    total = sum(p.value for p in cur.values()) or 1.0
    moves: list[Move] = []
    for cusip, pos in cur.items():
        prev = pri.get(cusip)
        if prev is None:
            action = "new"
        elif pos.shares > prev.shares * 1.001:
            action = "add"
        elif pos.shares < prev.shares * 0.999:
            action = "trim"
        else:
            continue  # unchanged — not a move
        moves.append(
            Move(
                cusip=cusip,
                name=pos.name,
                action=action,
                pct_of_book=pos.value / total,
                shares=pos.shares,
            )
        )
    for cusip, prev in pri.items():
        if cusip not in cur:
            moves.append(
                Move(cusip=cusip, name=prev.name, action="exit", pct_of_book=0.0, shares=0.0)
            )
    return moves


# ---------------------------------------------------------------------------
# CUSIP / name → ticker (resolve a holding to a universe ticker)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UniverseResolver:
    """Maps a 13F holding to a tracked-universe ticker. CUSIP is the precise
    key (from the local FMP profile caches); the normalized issuer name is the
    fallback (reusing the adjacency lexicon's name normalization)."""

    by_cusip: dict[str, tuple[str, bool]] = field(default_factory=dict[str, tuple[str, bool]])
    by_name: dict[tuple[str, ...], tuple[str, bool]] = field(
        default_factory=dict[tuple[str, ...], tuple[str, bool]]
    )

    def resolve(self, cusip: str, name: str) -> tuple[str, bool] | None:
        """Return (ticker, tracked) or None when the holding isn't in our
        universe. ``tracked`` is True for the active book (portfolio/evaluation),
        False for a discoverable index member."""
        hit = self.by_cusip.get(cusip.upper())
        if hit is not None:
            return hit
        from discovery.adjacency import normalize_phrase

        return self.by_name.get(normalize_phrase(name))


def build_universe_resolver(db_path: Path, fmp_dir: Path) -> UniverseResolver:
    """Build the resolver from ``tracked_companies`` + their local FMP profile
    caches. ``index_member`` names are discoverable (tracked=False); every other
    list (portfolio/evaluation/watchlist) is the active book (tracked=True).

    Reads only the profiles of the tracked names (a known list — never globs the
    110k-file FMP cache)."""

    from discovery.adjacency import normalize_phrase

    resolver = UniverseResolver()
    if not db_path.exists():
        return resolver
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return resolver
    try:
        rows = conn.execute("SELECT ticker, name, list_type FROM tracked_companies").fetchall()
    except sqlite3.Error:
        return resolver
    finally:
        conn.close()
    for raw_ticker, raw_name, raw_list in rows:
        ticker = str(raw_ticker).upper()
        tracked = str(raw_list) != "index_member"
        name = str(raw_name or "")
        norm = normalize_phrase(name)
        if norm:
            resolver.by_name.setdefault(norm, (ticker, tracked))
        cusip = _profile_cusip(fmp_dir, ticker)
        if cusip:
            resolver.by_cusip[cusip] = (ticker, tracked)
    return resolver


def _profile_cusip(fmp_dir: Path, ticker: str) -> str | None:
    path = fmp_dir / f"{ticker.upper()}_profile.json"
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rec_obj: object = raw
    if isinstance(raw, list):
        rows = cast("list[object]", raw)
        if not rows:
            return None
        rec_obj = rows[0]
    if not isinstance(rec_obj, dict):
        return None
    cusip = cast("dict[str, object]", rec_obj).get("cusip")
    return str(cusip).upper() if isinstance(cusip, str) and cusip.strip() else None


# ---------------------------------------------------------------------------
# SEC fetch (throttled + on-disk cache; mocked in tests)
# ---------------------------------------------------------------------------

_last_request = 0.0


def _throttle() -> None:
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
    _last_request = time.monotonic()


def _sec_get(url: str, *, user_agent: str, as_json: bool) -> object | str | None:
    """One throttled SEC GET. Returns parsed JSON / text, or None on any
    failure (the caller degrades per manager)."""
    _throttle()
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning({"event": "thirteenf_fetch_failed", "url": url, "error": str(exc)[:200]})
        return None
    if as_json:
        try:
            return resp.json()
        except ValueError:
            return None
    return resp.text


def _recent_13f_accessions(submissions: object, limit: int = 2) -> list[tuple[str, str]]:
    """The ``limit`` most recent (filing_date, accession) 13F-HR filings,
    newest first, from a submissions JSON payload."""
    if not isinstance(submissions, dict):
        return []
    filings = cast("dict[str, object]", submissions).get("filings")
    recent = cast("dict[str, object]", filings).get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        return []
    r = cast("dict[str, object]", recent)

    def _col(key: str) -> list[str]:
        v = r.get(key)
        return [str(x) for x in cast("list[object]", v)] if isinstance(v, list) else []

    forms, accs, dates = _col("form"), _col("accessionNumber"), _col("filingDate")
    hits = [
        (dates[i] if i < len(dates) else "", accs[i] if i < len(accs) else "")
        for i, f in enumerate(forms)
        if f == "13F-HR" and i < len(accs)
    ]
    hits = [(d, a) for d, a in hits if d and a]
    hits.sort(reverse=True)
    return hits[:limit]


def _info_table_name(index_payload: object) -> str | None:
    if not isinstance(index_payload, dict):
        return None
    directory = cast("dict[str, object]", index_payload).get("directory")
    items = (
        cast("dict[str, object]", directory).get("item") if isinstance(directory, dict) else None
    )
    if not isinstance(items, list):
        return None
    for it in cast("list[object]", items):
        if isinstance(it, dict):
            nm = str(cast("dict[str, object]", it).get("name") or "")
            if "infotable" in nm.lower() and nm.lower().endswith(".xml"):
                return nm
    return None


def _fetch_positions(
    cik: str, filing_date: str, accession: str, *, user_agent: str
) -> list[Position]:
    acc = accession.replace("-", "")
    cik_int = int(cik)
    index = _sec_get(
        EDGAR_INDEX_URL.format(cik_int=cik_int, acc=acc), user_agent=user_agent, as_json=True
    )
    name = _info_table_name(index)
    if name is None:
        log.warning({"event": "thirteenf_no_infotable", "cik": cik, "accession": accession})
        return []
    xml = _sec_get(
        EDGAR_FILE_URL.format(cik_int=cik_int, acc=acc, name=name),
        user_agent=user_agent,
        as_json=False,
    )
    return parse_info_table(xml) if isinstance(xml, str) else []


def mine_manager(
    cik: str, source_key: str, *, resolver: UniverseResolver, user_agent: str = DEFAULT_USER_AGENT
) -> list[InvestorHit]:
    """One manager: fetch the two latest 13F-HRs, diff, resolve each buy to a
    universe ticker. Emits a hit per ``new``/``add`` on a resolvable name
    (sells aren't discovery signals). Best-effort: [] on any fetch failure."""
    submissions = _sec_get(
        EDGAR_SUBMISSIONS_URL.format(cik=cik), user_agent=user_agent, as_json=True
    )
    accessions = _recent_13f_accessions(submissions, limit=2)
    if not accessions:
        return []
    cur_date, cur_acc = accessions[0]
    current = _fetch_positions(cik, cur_date, cur_acc, user_agent=user_agent)
    prior = (
        _fetch_positions(cik, accessions[1][0], accessions[1][1], user_agent=user_agent)
        if len(accessions) > 1
        else []
    )
    if not current:
        return []
    filing_url = EDGAR_FILE_URL.format(cik_int=int(cik), acc=cur_acc.replace("-", ""), name="")
    hits: list[InvestorHit] = []
    for move in diff_positions(current, prior):
        if move.action not in ("new", "add") or move.pct_of_book < MIN_POSITION_PCT:
            continue
        resolved = resolver.resolve(move.cusip, move.name)
        if resolved is None:
            continue
        ticker, tracked = resolved
        verb = "NEW" if move.action == "new" else "added to"
        hits.append(
            InvestorHit(
                ticker=ticker,
                name=move.name or None,
                tracked=tracked,
                source_key=source_key,
                action=move.action,
                pct_of_book=move.pct_of_book,
                observed_at=cur_date,
                detail=f"{verb} {move.pct_of_book * 100:.1f}% position ({cur_date})",
                filing_url=filing_url,
            )
        )
    return hits


def mine_13f(
    db_path: Path,
    fmp_dir: Path,
    *,
    sources: list[tuple[str, str]],
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[InvestorHit]:
    """Mine every rostered manager with a CIK. ``sources`` is a list of
    ``(source_key, cik)`` (the caller filters ``discovery_sources`` to
    ``active = 1 AND cik IS NOT NULL``). Sequential — the SEC throttle makes
    parallelism pointless."""
    resolver = build_universe_resolver(db_path, fmp_dir)
    if not resolver.by_cusip and not resolver.by_name:
        return []
    all_hits: list[InvestorHit] = []
    for source_key, cik in sources:
        all_hits.extend(mine_manager(cik, source_key, resolver=resolver, user_agent=user_agent))
    log.info({"event": "thirteenf_mine_done", "managers": len(sources), "hits": len(all_hits)})
    return all_hits

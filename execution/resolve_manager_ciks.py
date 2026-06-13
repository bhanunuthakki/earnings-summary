"""execution/resolve_manager_ciks.py — resolve rostered 13F managers' CIKs.

The 13F miner (``thirteenf.py``) only polls ``discovery_sources`` rows that
carry a ``cik``. Only Appaloosa's shipped verified in the seed; this resolves
the rest from EDGAR — SAFELY, because blind name matching is a trap:

  * a manager's OLD entity keeps a CIK but stopped filing (Viking Global
    Equities LP's last 13F-HR is 2002), and
  * a firm files several sleeves under near-identical names (Sands Capital
    *Alternatives* vs the Select Growth book; Polen *Credit* vs Focus Growth).

So a CIK is only auto-applied when there is EXACTLY ONE candidate that both
(a) filed a 13F-HR recently (within ``RECENCY_DAYS``) and (b) matches the
roster name strongly. Anything ambiguous (several recent strong matches) or
stale (no recent filing) is REPORTED for manual disposition, never guessed —
the owner pastes the right CIK via the Sources panel (``set_source_cik``).

Network is injected (``fetch``) so the resolution logic is unit-tested without
SEC calls. Dry-run by default; ``--apply`` writes the confident matches.

Usage:
    python execution/resolve_manager_ciks.py            # dry-run, all unresolved
    python execution/resolve_manager_ciks.py --apply
    python execution/resolve_manager_ciks.py --source-key sands   # one fund
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from discovery.sources import SourceRow, list_sources, set_source_cik  # noqa: E402

_UA = "earnings-summary/1.0 (personal research; bhanumufcpraneeth@gmail.com)"
_SEARCH_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={q}"
    "&type=13F-HR&dateb=&owner=include&count=10&output=atom"
)
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

#: A candidate must have filed a 13F-HR within this many days to be "active".
RECENCY_DAYS = 250
#: Minimum roster-name ↔ EDGAR-name token overlap to count as a match.
NAME_MATCH_MIN = 0.6
_MIN_REQUEST_INTERVAL_S = 0.4

# Tokens dropped before the name-overlap score (legal forms + generic fund words
# that don't distinguish one manager from another).
_STOP_TOKENS = frozenset(
    {
        "lp",
        "llc",
        "l",
        "p",
        "inc",
        "ltd",
        "llp",
        "the",
        "co",
        "company",
        "capital",
        "management",
        "mgmt",
        "investment",
        "investments",
        "advisers",
        "advisors",
        "partners",
        "group",
        "associates",
        "global",
        "fund",
        "funds",
    }
)

Fetch = Callable[[str], "str | None"]


@dataclass(frozen=True, slots=True)
class Candidate:
    cik: str
    edgar_name: str
    latest_13f: str | None
    name_score: float

    def is_recent(self, as_of: date) -> bool:
        if not self.latest_13f:
            return False
        try:
            filed = datetime.strptime(self.latest_13f, "%Y-%m-%d").date()
        except ValueError:
            return False
        return (as_of - filed).days <= RECENCY_DAYS


@dataclass(frozen=True, slots=True)
class Resolution:
    source_key: str
    cik: str | None  # set only when confidently resolved
    status: str  # 'resolved' | 'ambiguous' | 'stale' | 'not_found'
    detail: str


def _tokens(name: str) -> set[str]:
    raw = re.findall(r"[a-z0-9]+", name.lower())
    return {t for t in raw if t not in _STOP_TOKENS and len(t) > 1}


def name_score(roster_name: str, edgar_name: str) -> float:
    """Distinctive-token overlap (Jaccard over non-generic tokens). 'Sands
    Capital' vs 'Sands Capital Alternatives' → {sands} vs {sands, alternatives}
    = 0.5; an exact distinctive match → 1.0."""
    a, b = _tokens(roster_name), _tokens(edgar_name)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _search_query(display_name: str) -> str:
    """The roster display name → an EDGAR search string (drop the parenthetical
    principal, e.g. 'Appaloosa (Tepper)' → 'Appaloosa')."""
    return re.sub(r"\s*\(.*?\)\s*", " ", display_name).strip()


def resolve_one(source: SourceRow, *, fetch: Fetch, as_of: date | None = None) -> Resolution:
    """Resolve one manager. Confident ONLY when exactly one recent, strong-name
    candidate exists; otherwise ambiguous/stale/not_found (reported, not
    guessed)."""
    as_of = as_of or datetime.now(UTC).date()
    query = _search_query(source.display_name)
    atom = fetch(_SEARCH_URL.format(q=urllib.parse.quote(query)))
    ciks = re.findall(r"<CIK>(\d+)</CIK>", atom) if atom else []
    if not ciks:
        return Resolution(source.source_key, None, "not_found", f"no 13F filer for {query!r}")

    candidates: list[Candidate] = []
    for raw_cik in dict.fromkeys(ciks):  # de-dupe, preserve order
        cik = f"{int(raw_cik):010d}"
        payload = fetch(_SUBMISSIONS_URL.format(cik=cik))
        name, latest = _parse_submission(payload)
        candidates.append(Candidate(cik, name, latest, name_score(source.display_name, name)))

    recent_strong = [c for c in candidates if c.is_recent(as_of) and c.name_score >= NAME_MATCH_MIN]
    if len(recent_strong) == 1:
        c = recent_strong[0]
        return Resolution(
            source.source_key, c.cik, "resolved", f"{c.edgar_name} (13F {c.latest_13f})"
        )
    if len(recent_strong) > 1:
        names = "; ".join(f"{c.cik}={c.edgar_name}" for c in recent_strong)
        return Resolution(
            source.source_key, None, "ambiguous", f"{len(recent_strong)} matches: {names}"
        )
    # Strong by name but none recent → the entity stopped filing (old CIK).
    strong = [c for c in candidates if c.name_score >= NAME_MATCH_MIN]
    if strong:
        best = max(strong, key=lambda c: c.name_score)
        return Resolution(
            source.source_key, None, "stale", f"{best.edgar_name} last 13F {best.latest_13f}"
        )
    return Resolution(source.source_key, None, "not_found", f"no strong-name match for {query!r}")


def _parse_submission(payload: str | None) -> tuple[str, str | None]:
    """(conformed name, latest 13F-HR date) from a submissions JSON string."""
    if not payload:
        return "", None
    try:
        s = json.loads(payload)
    except ValueError:
        return "", None
    if not isinstance(s, dict):
        return "", None
    sub = cast("dict[str, object]", s)
    name = str(sub.get("name") or "")
    filings = sub.get("filings")
    recent = cast("dict[str, object]", filings).get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        return name, None
    rec = cast("dict[str, object]", recent)
    forms = rec.get("form")
    dates = rec.get("filingDate")
    if not isinstance(forms, list) or not isinstance(dates, list):
        return name, None
    hr = sorted(
        str(d)
        for f, d in zip(cast("list[object]", forms), cast("list[object]", dates), strict=False)
        if str(f) == "13F-HR" and isinstance(d, str)
    )
    return name, (hr[-1] if hr else None)


# ---------------------------------------------------------------------------
# Live SEC fetch (throttled) + the CLI
# ---------------------------------------------------------------------------

_last_request = 0.0


def _http_get(url: str) -> str | None:
    """One throttled SEC GET → text, or None on failure (the resolver treats a
    failed fetch as 'no data' for that candidate)."""
    global _last_request
    wait = _MIN_REQUEST_INTERVAL_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if "gzip" in resp.headers.get("Content-Encoding", "").lower():
                body = gzip.decompress(body)
            return body.decode("utf-8", errors="replace")
    except Exception:
        return None


def resolve_roster(
    *,
    source_key: str | None = None,
    apply: bool = False,
    fetch: Fetch = _http_get,
    db_path: Path | str | None = None,
    as_of: date | None = None,
) -> list[Resolution]:
    """Resolve every unresolved investor source (or one ``source_key``). Writes
    the confident matches when ``apply`` is set."""
    sources = [
        s
        for s in list_sources(signal_class="investor_13f", db_path=db_path)
        if (source_key is None and s.cik is None) or s.source_key == source_key
    ]
    results: list[Resolution] = []
    for source in sources:
        res = resolve_one(source, fetch=fetch, as_of=as_of)
        if res.status == "resolved" and apply and res.cik is not None:
            set_source_cik(res.source_key, res.cik, db_path=db_path)
        results.append(res)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else "")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--source-key", default=None, help="resolve just one fund")
    parser.add_argument("--apply", action="store_true", help="write the confident matches")
    args = parser.parse_args(argv)
    db_path = args.repo_root.resolve() / "data" / "portfolio.db"
    results = resolve_roster(source_key=args.source_key, apply=args.apply, db_path=db_path)
    for r in results:
        mark = "WROTE" if (args.apply and r.status == "resolved") else r.status.upper()
        print(f"{r.source_key:22s} [{mark}] {r.cik or '':>10s}  {r.detail}")
    resolved = sum(1 for r in results if r.status == "resolved")
    print(
        json.dumps(
            {
                "event": "resolve_manager_ciks_done",
                "total": len(results),
                "resolved": resolved,
                "applied": resolved if args.apply else 0,
                "needs_review": [r.source_key for r in results if r.status != "resolved"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

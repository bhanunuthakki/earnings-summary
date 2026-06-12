"""Industry classification + KPI-template loading.

Two responsibilities:

  1) **classify_ticker(ticker, repo_root)** — Deterministic rule-based industry
     bucket assignment. Uses (a) the entity row's `meta_json.sector` if the
     entity-spine has the ticker seeded, plus (b) name-pattern matches from
     entity_seed's PORTFOLIO_HOLDINGS / WATCHLIST_BIZ_MODELS. Returns the
     `industry` slug (e.g. "software_saas") or None when no rule matches —
     the caller is expected to skip auto-templating and let the human curate.

  2) **load_template(slug, repo_root)** — Read templates/industry/<slug>.yaml
     and return an `IndustryTemplate` with parsed canonical KPIs. The YAML is a
     hand-curated tier-1 KPI list per industry (the same shape that lives in
     `micro_thesis/holdings/<T>.json` under `tier_1_kpis`), so onboarding a
     new ticker doesn't require recreating 8+ KPI rows from memory.

Both functions are pure-deterministic; no LLM, no network. The classifier's
output is a *suggestion* — `onboard_ticker --industry-template auto` writes
nothing if classify returns None, falling back to the existing generic flow.

YAML parsing: deliberately scoped to the exact shape these templates use
(top-level scalars + one `canonical_kpis:` list of dicts). Avoids pulling in
PyYAML as a new dep. See `_parse_template_yaml` for the supported syntax.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CanonicalKPI:
    """One tier-1 KPI definition from an industry template."""

    name: str
    aliases: list[str] = field(default_factory=list)
    unit_kind: str | None = None
    expected_range: tuple[float | None, float | None] | None = None
    break_condition: str | None = None
    primary_source: str | None = None


@dataclass(slots=True)
class IndustryTemplate:
    """Loaded template for one industry bucket."""

    industry: str
    display_name: str
    canonical_kpis: list[CanonicalKPI]
    # Workspace section keys this business model should OMIT entirely (default:
    # none). The section-relevance map -- see SUPPRESSIBLE_SECTIONS and the
    # structural-review matrix below. (Typed factory keeps pyright strict from
    # inferring list[Unknown].)
    suppress_sections: list[str] = field(default_factory=list[str])

    def to_tier_1_kpis(self) -> list[dict[str, object]]:
        """Render canonical_kpis in the shape `micro_thesis/holdings/<T>.json`
        uses under `tier_1_kpis` (status=Unknown, current=TBV, etc.)."""
        out: list[dict[str, object]] = []
        for k in self.canonical_kpis:
            row: dict[str, object] = {
                "name": k.name,
                "current": "TBV from latest earnings release",
                "prior": None,
                "yoy": None,
                "status": "Unknown",
                "break_condition": k.break_condition or "",
                "source": k.primary_source or "earnings_release",
            }
            if k.aliases:
                row["aliases"] = list(k.aliases)
            if k.unit_kind:
                row["unit_kind"] = k.unit_kind
            out.append(row)
        return out


# ---------------------------------------------------------------------------
# Section-relevance map (workspace report)
# ---------------------------------------------------------------------------
#
# Some workspace panels are structurally irrelevant to certain business models:
# a neobank has no meaningful operating-lease ladder, a pure-software vendor has
# no named-customer concentration worth a panel, and so on. Rather than render
# an empty stub ("be smart about whether to display this element" -- NU report
# comment #15), the report builder asks suppressed_sections_for_ticker() which
# panels to OMIT, and the workspace renderer skips them.
#
# Canonical section keys: the vocabulary shared by (a) the `suppress_sections:`
# lists in templates/industry/*.yaml and (b) the gate checks in
# report/renderers/workspace_html.py:_company_tab. A test asserts both sides
# only ever use keys from SUPPRESSIBLE_SECTIONS so a typo can't silently fail to
# suppress.
SECTION_LEASE_LADDER = "lease_ladder"
SECTION_CUSTOMER_CONCENTRATION = "customer_concentration"
SECTION_STRATEGIC_TARGETS = "strategic_targets"

SUPPRESSIBLE_SECTIONS = frozenset(
    {SECTION_LEASE_LADDER, SECTION_CUSTOMER_CONCENTRATION, SECTION_STRATEGIC_TARGETS},
)

# Structural review -- which workspace panels belong in which company reports
# (the Opus enumeration requested in NU report comment #15). Only the three
# Company-tab P3 panels below vary by business model; every other workspace
# panel (thesis, earnings, financials, segments, valuation, bear case,
# decisions, say-do, exec comp, synthesis, sources) is model-agnostic and
# always renders.
#
#   panel                   bank  saas  hyper  pharma  royalty
#   ----------------------  ----  ----  -----  ------  -------
#   strategic_targets       show  show  show   show    show
#   customer_concentration  hide  show  show   show    show
#   lease_ladder            hide  hide  show   show    hide
#
# strategic_targets: universal -- every issuer sets long-term commitments
#   (banks: ROTCE/efficiency; software: ARR/margin; etc.).
# customer_concentration: a bank's revenue is net interest + fee income on a
#   diversified loan/deposit book, not named accounts, so the "customer >= 5% of
#   revenue" panel is always empty. Material for enterprise SaaS (whale accounts
#   >10%), pharma (a few wholesalers each >10%), and royalty/streaming (revenue
#   keyed to a handful of cornerstone assets/operators).
# lease_ladder: material where the P&L rides on leased physical capacity --
#   hyperscaler (data centers + fulfillment), pharma (plants/labs), and
#   retail/logistics. Immaterial for banks (branch leases are a rounding error
#   vs the loan book; a neobank has none), asset-light software (office leases
#   don't move the ARR/FCF thesis), and royalty/streaming (no operated assets).


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


# Hyperscaler is hardcoded: only AMZN, GOOG, GOOGL, MSFT, META qualify as
# cloud-platform owners with segmented cloud disclosure. Keeping it explicit
# avoids the false-positive risk of any "Cloud" name match flipping a SaaS
# vendor into the hyperscaler bucket.
_HYPERSCALER_TICKERS = frozenset({"AMZN", "GOOG", "GOOGL", "MSFT", "META"})

# Explicit software-SaaS ticker allowlist. The name-pattern matcher below
# misses most modern SaaS companies (CrowdStrike, ServiceNow, Datadog,
# MongoDB, Snowflake etc. don't have "software/cloud/saas" literally in the
# legal entity name), so we maintain this curated list for the canonical
# cases. Extend it when new SaaS tickers are added to entity_seed.
_SOFTWARE_SAAS_TICKERS = frozenset(
    {
        "CFLT",
        "CRM",
        "CRWD",
        "DDOG",
        "ESTC",
        "FTNT",
        "GTLB",
        "KVYO",
        "MDB",
        "NET",
        "NOW",
        "OKTA",
        "PANW",
        "RBRK",
        "SNOW",
        "VEEV",
        "WIX",
        "ZS",
    },
)

# Sector → industry rules. Order matters: the first matching rule wins so the
# specific cases (hyperscaler, royalty/streaming) come before the catch-all
# Technology / Materials buckets.
_SECTOR_TECH = {"Technology", "Information Technology"}
_SECTOR_FIN = {"Financials", "Financial Services"}
_SECTOR_HEALTH = {"Health Care", "Healthcare"}
_SECTOR_MATERIALS = {"Materials", "Basic Materials"}
_SECTOR_COMMS = {"Communication Services", "Communications"}

# Name-pattern keywords for industry hints. The classifier checks the issuer's
# canonical/display name in addition to sector — a ticker like NU (Nubank) is
# in Financials and matches /bank/i, so the bank template fires.
_SOFTWARE_HINTS = re.compile(
    r"\b(software|cloud|saas|platform|cybersecurity|security|analytics|"
    r"observabil|data\s*lake|database|devops|workflow|automation|"
    r"crm|erp|app[s]?)\b",
    re.IGNORECASE,
)
_BANK_HINTS = re.compile(
    r"\b(bank|banco|nubank|holdings\s+ltd|financial\s+corp|"
    r"bancorp|trust\s+co|credit\s+union)\b",
    re.IGNORECASE,
)
_PHARMA_HINTS = re.compile(
    r"\b(pharma|pharmaceut|biotech|biopharm|therapeutic|"
    r"oncology|drug|novo\s*nordisk|novartis|lilly|merck|"
    r"pfizer|sanofi|roche|astrazeneca)\b",
    re.IGNORECASE,
)
_ROYALTY_HINTS = re.compile(
    r"\b(royalt|streaming|franco[\s-]*nevada|wheaton\s+precious|"
    r"sandstorm|metals?\s+royalt|osisko|maverix)\b",
    re.IGNORECASE,
)


def classify_ticker(ticker: str, repo_root: Path) -> str | None:
    """Return the industry slug for `ticker`, or None if no rule matches.

    Resolution order:
      1) AMZN/GOOG/GOOGL/MSFT/META → hyperscaler (hardcoded)
      2) Look up sector + name from entity row (meta_json.sector + canonical_name)
      3) Fall back to entity_seed's hand-curated registry (sector + name)
      4) Apply name-pattern + sector matchers; return the first hit
    """
    t = ticker.upper().strip()
    if not t:
        return None
    if t in _HYPERSCALER_TICKERS:
        return "hyperscaler"
    if t in _SOFTWARE_SAAS_TICKERS:
        return "software_saas"

    sector, name = _lookup_sector_name(t, repo_root)
    return _apply_rules(sector=sector, name=name)


def _apply_rules(*, sector: str | None, name: str | None) -> str | None:
    """Pure function: given sector + name, return the industry slug."""
    sector_norm = (sector or "").strip()
    name_norm = (name or "").strip()

    # Materials → royalty/streaming wins over a generic miner; we only want
    # the template for royalty/streaming-shape businesses (low capex per oz,
    # exposure to commodity price). A pure miner like FCX/IVN/RIO doesn't fit
    # this template; keep them None and let the human curate.
    if sector_norm in _SECTOR_MATERIALS and _ROYALTY_HINTS.search(name_norm):
        return "commodity_royalty"

    # Health Care → pharma (only if the name signals pharma; medical-device or
    # health-services companies like ISRG/DHR shouldn't get the pharma KPIs)
    if sector_norm in _SECTOR_HEALTH and _PHARMA_HINTS.search(name_norm):
        return "pharma"

    # Financials → bank (only if the name signals bank; asset managers,
    # exchanges, payment networks don't fit this template)
    if sector_norm in _SECTOR_FIN and _BANK_HINTS.search(name_norm):
        return "bank"

    # Technology + Communication Services → software_saas (broad bucket; the
    # tier-1 KPIs are conservatively-applicable to most software business
    # models. Hyperscalers were already caught above.)
    if (sector_norm in _SECTOR_TECH or sector_norm in _SECTOR_COMMS) and _SOFTWARE_HINTS.search(
        name_norm,
    ):
        return "software_saas"

    return None


def _lookup_sector_name(ticker: str, repo_root: Path) -> tuple[str | None, str | None]:
    """Return (sector, name) for ticker.

    Checks (in order):
      1) entities table — meta_json.sector + canonical_name where
         external_ids.ticker = ?
      2) entity_seed.PORTFOLIO_HOLDINGS / WATCHLIST_BIZ_MODELS

    Both can fail (no DB, missing entity row, ticker not seeded). Returns
    (None, None) in that case — the caller's matcher just returns None too.
    """
    db_sector, db_name = _lookup_sector_from_db(ticker, repo_root)
    if db_sector is not None or db_name is not None:
        return (db_sector, db_name)
    return _lookup_sector_from_seed(ticker)


def _lookup_sector_from_db(
    ticker: str,
    repo_root: Path,
) -> tuple[str | None, str | None]:
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return (None, None)
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Verify entities table exists before querying
        check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entities'",
        ).fetchone()
        if check is None:
            conn.close()
            return (None, None)
        row = conn.execute(
            """
            SELECT canonical_name, display_name, meta_json
            FROM entities
            WHERE kind = 'company'
              AND json_extract(external_ids, '$.ticker') = ?
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        conn.close()
        if row is None:
            return (None, None)
        sector: str | None = None
        meta_raw = row["meta_json"]
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
                if isinstance(meta, dict):
                    val = cast("dict[str, object]", meta).get("sector")
                    if isinstance(val, str):
                        sector = val
            except (json.JSONDecodeError, TypeError):
                pass
        name = row["canonical_name"] or row["display_name"]
        return (sector, name)
    except sqlite3.Error as exc:
        log.debug({"event": "industry_classify_db_error", "ticker": ticker, "error": str(exc)})
        return (None, None)


def _lookup_sector_from_seed(ticker: str) -> tuple[str | None, str | None]:
    """Fall back to entity_seed's hand-curated registry."""
    try:
        from entity_seed import PORTFOLIO_HOLDINGS, WATCHLIST_BIZ_MODELS
    except ImportError:
        return (None, None)
    if ticker in PORTFOLIO_HOLDINGS:
        seed = PORTFOLIO_HOLDINGS[ticker]
        return (seed.sector, seed.canonical_name)
    if ticker in WATCHLIST_BIZ_MODELS:
        meta = WATCHLIST_BIZ_MODELS[ticker]
        return (meta.get("sector"), meta.get("name"))
    return (None, None)


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------


def load_template(industry_slug: str, repo_root: Path) -> IndustryTemplate:
    """Load templates/industry/<slug>.yaml and parse into IndustryTemplate.

    Raises FileNotFoundError if the slug doesn't have a YAML; ValueError if the
    YAML doesn't parse to the expected shape. These are loud errors by design —
    a typo in --industry-template should fail the onboarding immediately, not
    silently produce a holdings JSON with no tier-1 KPIs.
    """
    path = repo_root / "templates" / "industry" / f"{industry_slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No industry template found at {path!s}. "
            f"Available slugs: {available_industries(repo_root)}",
        )
    raw = path.read_text(encoding="utf-8")
    parsed = _parse_template_yaml(raw)
    return _build_template(parsed, source_path=path)


def available_industries(repo_root: Path) -> list[str]:
    """Return all industry slugs that have a YAML in templates/industry/."""
    dir_ = repo_root / "templates" / "industry"
    if not dir_.exists():
        return []
    return sorted(p.stem for p in dir_.glob("*.yaml"))


def suppressed_sections_for_ticker(ticker: str, repo_root: Path) -> frozenset[str]:
    """Workspace section keys to OMIT for `ticker`, per its business model.

    Resolves the industry via `classify_ticker`, then reads that template's
    `suppress_sections`. DEFAULT TO SHOW: returns an empty set when the ticker
    is unclassified (no rule matches) or its template can't be loaded, so an
    unseeded ticker renders every panel rather than being silently blanked.

    Pure-deterministic (classify + read one YAML); no LLM, no network. Computed
    once in the report builder and threaded onto ReportSpec.suppressed_sections;
    the workspace renderer gates the Company-tab P3 panels on the result.
    """
    slug = classify_ticker(ticker, repo_root)
    if slug is None:
        return frozenset()
    try:
        template = load_template(slug, repo_root)
    except (FileNotFoundError, ValueError) as exc:
        log.debug(
            {
                "event": "suppress_sections_template_load_failed",
                "ticker": ticker,
                "slug": slug,
                "error": str(exc),
            },
        )
        return frozenset()
    return frozenset(template.suppress_sections)


def _build_template(parsed: dict[str, object], *, source_path: Path) -> IndustryTemplate:
    industry = parsed.get("industry")
    if not isinstance(industry, str):
        raise ValueError(f"{source_path.name}: missing or non-string 'industry' field")
    display_name = parsed.get("display_name")
    if not isinstance(display_name, str):
        raise ValueError(f"{source_path.name}: missing or non-string 'display_name' field")
    kpis_raw = parsed.get("canonical_kpis")
    if not isinstance(kpis_raw, list):
        raise ValueError(f"{source_path.name}: missing or non-list 'canonical_kpis' field")
    kpis: list[CanonicalKPI] = []
    for idx, item in enumerate(cast("list[object]", kpis_raw)):
        if not isinstance(item, dict):
            raise ValueError(f"{source_path.name}: canonical_kpis[{idx}] is not a dict")
        item_dict = cast("dict[str, object]", item)
        kpi_name = item_dict.get("name")
        if not isinstance(kpi_name, str):
            raise ValueError(
                f"{source_path.name}: canonical_kpis[{idx}] missing 'name'",
            )
        aliases_obj = item_dict.get("aliases") or []
        if not isinstance(aliases_obj, list):
            raise ValueError(
                f"{source_path.name}: canonical_kpis[{idx}].aliases is not a list",
            )
        aliases_list = [str(a) for a in cast("list[object]", aliases_obj)]
        unit_kind_obj = item_dict.get("unit_kind")
        unit_kind = unit_kind_obj if isinstance(unit_kind_obj, str) else None
        break_obj = item_dict.get("break_condition")
        break_cond = break_obj if isinstance(break_obj, str) else None
        primary_obj = item_dict.get("primary_source")
        primary = primary_obj if isinstance(primary_obj, str) else None
        expected_range = _parse_expected_range(item_dict.get("expected_range"))
        kpis.append(
            CanonicalKPI(
                name=kpi_name,
                aliases=aliases_list,
                unit_kind=unit_kind,
                expected_range=expected_range,
                break_condition=break_cond,
                primary_source=primary,
            ),
        )
    suppress_raw = parsed.get("suppress_sections")
    suppress_sections: list[str] = []
    if isinstance(suppress_raw, list):
        suppress_sections = [str(s) for s in cast("list[object]", suppress_raw)]
    return IndustryTemplate(
        industry=industry,
        display_name=display_name,
        canonical_kpis=kpis,
        suppress_sections=suppress_sections,
    )


def _parse_expected_range(raw: object) -> tuple[float | None, float | None] | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    raw_list = cast("list[object]", raw)
    lo_raw, hi_raw = raw_list[0], raw_list[1]
    lo = float(lo_raw) if isinstance(lo_raw, (int, float)) else None
    hi = float(hi_raw) if isinstance(hi_raw, (int, float)) else None
    return (lo, hi)


# ---------------------------------------------------------------------------
# Minimal YAML reader
# ---------------------------------------------------------------------------
#
# This is NOT a general YAML parser. It handles the exact subset our templates
# use: a 2-space-indented mapping at the root, where:
#
#   - top-level scalars are `key: value` (value may be quoted)
#   - top-level lists are `key:\n  - item1\n  - item2`
#   - list items can themselves be mappings (4-space indent), with their first
#     `key: value` introduced by `- name: ...` and subsequent keys by `    key: ...`
#   - inline bracketed lists [a, b, c] supported for short scalar lists
#   - inline `[null, null]` and `[1.5, 2.0]` supported for `expected_range`
#
# If we ever need richer YAML (anchors, multiline strings), pull in PyYAML.


_INLINE_LIST_RX = re.compile(r"^\[(.*)\]$")
_BARE_SCALAR_RX = re.compile(r"^(?:null|true|false|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)$")


def _parse_template_yaml(raw: str) -> dict[str, object]:
    """Parse the strict-subset YAML used by industry templates."""
    lines = raw.splitlines()
    # Strip comments + blank lines
    cleaned: list[str] = []
    for line in lines:
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        cleaned.append(stripped)

    result: dict[str, object] = {}
    i = 0
    while i < len(cleaned):
        line = cleaned[i]
        if _indent_of(line) != 0:
            raise ValueError(f"unexpected indented line at top level: {line!r}")
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val == "":
            # Block follows — either a list or a nested mapping
            i += 1
            block, consumed = _parse_block(cleaned, i, parent_indent=0)
            result[key] = block
            i += consumed
        else:
            result[key] = _parse_scalar(val)
            i += 1
    return result


def _parse_block(
    lines: list[str],
    start: int,
    *,
    parent_indent: int,
) -> tuple[object, int]:
    """Parse an indented block starting at lines[start].

    Returns (block_value, lines_consumed). The block is either a list (if the
    first line starts with `-`) or a mapping (if it's `key: ...`).
    """
    if start >= len(lines):
        return ([], 0)
    first = lines[start]
    first_indent = _indent_of(first)
    if first_indent <= parent_indent:
        return ([], 0)
    if first.lstrip().startswith("- "):
        return _parse_list(lines, start, list_indent=first_indent)
    return _parse_mapping(lines, start, map_indent=first_indent)


def _parse_list(
    lines: list[str],
    start: int,
    *,
    list_indent: int,
) -> tuple[list[object], int]:
    items: list[object] = []
    i = start
    while i < len(lines):
        line = lines[i]
        cur_indent = _indent_of(line)
        if cur_indent < list_indent:
            break
        if cur_indent > list_indent:
            # A continuation of a list-item-mapping that's already been opened
            # at exactly list_indent; should never happen because we consume
            # those in the inner mapping pass below.
            break
        stripped = line.lstrip()
        if not stripped.startswith("-"):
            break
        # Two shapes:
        #   - scalar
        #   - key: value     (start of a mapping item)
        rest = stripped[1:].lstrip()
        if ":" in rest and not (rest.startswith("'") or rest.startswith('"')):
            # mapping item — parse the first key:value, then look for more
            # keys at indent list_indent + 2 (under the dash, 2 spaces deeper)
            inner_indent = list_indent + 2
            first_key, _, first_val = rest.partition(":")
            first_key = first_key.strip()
            first_val = first_val.strip()
            item: dict[str, object] = {}
            if first_val == "":
                # Nested block follows
                i += 1
                block, consumed = _parse_block(lines, i, parent_indent=inner_indent - 1)
                item[first_key] = block
                i += consumed
            else:
                item[first_key] = _parse_scalar(first_val)
                i += 1
            # Subsequent keys for this same item appear at `inner_indent`
            while i < len(lines):
                next_line = lines[i]
                if _indent_of(next_line) != inner_indent or next_line.lstrip().startswith("-"):
                    break
                k, _, v = next_line.partition(":")
                k = k.strip()
                v = v.strip()
                if v == "":
                    i += 1
                    block, consumed = _parse_block(lines, i, parent_indent=inner_indent)
                    item[k] = block
                    i += consumed
                else:
                    item[k] = _parse_scalar(v)
                    i += 1
            items.append(item)
        else:
            # bare scalar item
            items.append(_parse_scalar(rest))
            i += 1
    return (items, i - start)


def _parse_mapping(
    lines: list[str],
    start: int,
    *,
    map_indent: int,
) -> tuple[dict[str, object], int]:
    result: dict[str, object] = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if _indent_of(line) != map_indent:
            break
        if line.lstrip().startswith("-"):
            break
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v == "":
            i += 1
            block, consumed = _parse_block(lines, i, parent_indent=map_indent)
            result[k] = block
            i += consumed
        else:
            result[k] = _parse_scalar(v)
            i += 1
    return (result, i - start)


def _parse_scalar(raw: str) -> object:
    """Parse one scalar value: quoted string, bare number, null/true/false,
    or inline-bracketed list."""
    s = raw.strip()
    if not s:
        return ""
    # Inline list: [a, b, c]
    m = _INLINE_LIST_RX.match(s)
    if m is not None:
        body = m.group(1)
        if body.strip() == "":
            return []
        parts = _split_inline_list(body)
        return [_parse_scalar(p.strip()) for p in parts]
    # Quoted string
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    # null / true / false
    if s.lower() == "null":
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    # number?
    if _BARE_SCALAR_RX.match(s):
        if "." in s or "e" in s.lower():
            return float(s)
        try:
            return int(s)
        except ValueError:
            return s
    return s


def _split_inline_list(body: str) -> list[str]:
    """Split an inline bracket list by commas, respecting quoted strings."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in body:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))

"""Minimal Markdown renderer for the ETF brief.

Mirrors the conventions of `report.renderers.markdown` (one StringIO,
section-by-section, missing-block fallbacks) but emits the ETF-specific
shape: §1 Profile + §5 Holdings. Future sections (Premium-discount band,
look-through overlap with equity holdings, rotation lens) will graduate
into their own helpers as ETF coverage deepens.
"""

from __future__ import annotations

from io import StringIO

from report.etf_models import (
    EtfBriefSpec,
    EtfHoldingsSection,
    EtfProfileSection,
)
from report.models import MissingReason, SectionStatus


def render(spec: EtfBriefSpec) -> str:
    out = StringIO()
    out.write(f"# {spec.ticker} — ETF Brief\n\n")
    out.write(f"_Generated {spec.generation_date.isoformat()}_\n\n")

    _profile(out, spec.profile)
    _holdings(out, spec.holdings)

    return out.getvalue()


def _profile(out: StringIO, section: EtfProfileSection) -> None:
    out.write("## §1 ETF Profile\n\n")
    if _emit_missing(out, section.status, section.missing):
        return

    rows: list[tuple[str, str]] = [("Name", section.name or "—")]
    if section.issuer:
        rows.append(("Issuer", section.issuer))
    if section.benchmark_index:
        rows.append(("Benchmark", section.benchmark_index))
    if section.asset_class:
        rows.append(("Asset class", section.asset_class))
    if section.sector_label:
        rows.append(("Sector", section.sector_label))
    if section.listed_exchange:
        rows.append(("Exchange", section.listed_exchange))
    if section.domicile:
        rows.append(("Domicile", section.domicile))
    if section.inception_date:
        rows.append(("Inception", section.inception_date.isoformat()))
    if section.expense_ratio is not None:
        rows.append(("Expense ratio", _bps(section.expense_ratio)))
    if section.aum_usd_m is not None:
        rows.append(("AUM", _aum(section.aum_usd_m)))
    if section.distribution_yield is not None:
        rows.append(("Distribution yield", _pct(section.distribution_yield)))
    if section.nav is not None:
        rows.append(("NAV", f"${section.nav:.2f}"))
    if section.price is not None:
        rows.append(("Close price", f"${section.price:.2f}"))
    if section.premium_discount_pct is not None:
        rows.append(("Premium / discount", _pct(section.premium_discount_pct)))

    out.write("| Field | Value |\n|---|---|\n")
    for label, value in rows:
        out.write(f"| {label} | {value} |\n")
    out.write("\n")
    if section.description:
        out.write("### Description\n\n")
        out.write(section.description.strip() + "\n\n")


def _holdings(out: StringIO, section: EtfHoldingsSection) -> None:
    out.write("## §5 Holdings\n\n")
    if _emit_missing(out, section.status, section.missing):
        return

    out.write(
        f"_As of {section.as_of_date.isoformat() if section.as_of_date else '—'}, "
        f"{section.total_constituents} total constituents_\n\n"
    )

    if section.top_holdings:
        out.write("### Top holdings\n\n")
        out.write("| # | Ticker | Name | Weight | Sector |\n")
        out.write("|---|---|---|---|---|\n")
        for h in section.top_holdings:
            out.write(
                f"| {h.rank or ''} | {h.constituent_ticker or '—'} | "
                f"{h.name or '—'} | {_pct(h.weight_pct)} | {h.sector or '—'} |\n"
            )
        out.write("\n")

    if section.sector_breakdown:
        out.write("### Sector breakdown\n\n")
        out.write("| Sector | Weight |\n|---|---|\n")
        for row in section.sector_breakdown:
            out.write(f"| {row.sector} | {_pct(row.weight_pct)} |\n")
        out.write("\n")


def _emit_missing(out: StringIO, status: SectionStatus, missing: MissingReason | None) -> bool:
    """If status is missing/partial, emit the placeholder block. Return True if emitted."""
    if status == SectionStatus.OK:
        return False
    label = {
        SectionStatus.MISSING_DATA: "Missing data",
        SectionStatus.PARTIAL: "Partial data",
        SectionStatus.LLM_PENDING: "LLM pending",
        SectionStatus.NOT_APPLICABLE: "Not applicable",
    }.get(status, str(status))
    out.write(f"> _{label}_")
    if missing is not None:
        out.write(f" — stage `{missing.stage}`; fix: `{missing.fix_command}`")
    out.write("\n\n")
    return True


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _bps(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 10000:.0f} bps"


def _aum(value_m: float | None) -> str:
    if value_m is None:
        return "—"
    if value_m >= 1000:
        return f"${value_m / 1000:.2f}B"
    return f"${value_m:.0f}M"

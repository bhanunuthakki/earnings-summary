"""The ETF workup peek — the fund's full evaluation story in one fragment.

The click-through behind the evaluation row's ETF pill
(``GET /api/peek/etf_workup``). Five blocks, all disk/DB reads (the render
path never runs a Sharpe window, an OLS, or an LLM):

  ① profile strip — ER (bps), AUM, issuer, benchmark, basket P/E-P/B with
     their as-of + source (etf_profile, published-data layer);
  ② style loadings — the three spread betas + r² from the Stage 0f cache;
  ③ look-through — direct overlap %, the top shared names (ETF w% vs book
     w%), and the country exposure rollup (N-PORT invCountry);
  ④ what-if at weight — the precomputed 1/3/5% before→after rows;
  ⑤ role in portfolio — the governed one-pager (etf_role_synthesis
     artifact), with the build-hint CLI when absent.

Every block degrades independently with an explicit missing state — a fund
with no holdings snapshot still shows its profile and what-if.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from etf_overlap import EtfOverlap, compute_lookthrough_overlap
from etf_role_synthesis import read_role_synthesis, synthesis_generated_at
from etf_score_cache import read_materialized_etf_loadings, read_materialized_etf_whatif
from instrument_store import get_etf_profile
from portfolio_weights import read_materialized_weights
from ui.prose import render_prose

_WORKUP_CSS = """<style>
.etfw { display: flex; flex-direction: column; gap: 12px; }
.etfw h4 { margin: 0 0 4px; font-size: var(--fs-caption); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; }
.etfw-strip { display: flex; flex-wrap: wrap; gap: 6px; }
.etfw-row { display: grid; grid-template-columns: 120px 1fr; gap: 8px; padding: 4px 0;
  border-bottom: 1px solid var(--hairline); font-size: var(--fs-body); }
.etfw-row:last-child { border-bottom: none; }
.etfw-row .v { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.etfw-table { width: 100%; border-collapse: collapse; font-size: var(--fs-body); }
.etfw-table td, .etfw-table th { padding: 4px 8px 4px 0; text-align: left; }
.etfw-table td.num { font-family: var(--mono); }
.etfw-miss { color: var(--muted); font-size: var(--fs-body); }
.etfw-src { color: var(--muted); font-size: var(--fs-caption); }
.etfw-verdict { margin-bottom: 6px; }
</style>"""

_VERDICT_TONES: dict[str, str] = {
    "closes_target_gap": "k-pill k-pill-ok",
    "diversifier": "k-pill k-pill-ok",
    "redundant": "k-pill k-pill-warn",
    "style_crowding": "k-pill k-pill-warn",
    "insufficient_data": "k-pill",
}


def render_etf_workup(
    conn: sqlite3.Connection, repo_root: Path, db_path: Path, ticker: str
) -> str | None:
    """The workup fragment; None when the ticker isn't a tracked ETF (404)."""
    t = ticker.strip().upper()
    if not _is_tracked_etf(conn, t):
        return None
    weights = read_materialized_weights(repo_root)
    overlap = compute_lookthrough_overlap(conn, t, weights) if weights else None
    blocks = [
        _profile_block(conn, t),
        _loadings_block(repo_root, t),
        _overlap_block(overlap, bool(weights)),
        _whatif_block(repo_root, t),
        _role_block(db_path, t),
    ]
    foot = (
        f'<div class="cc-peek-foot"><a href="/ticker/{escape(t, quote=True)}">'
        "open the holding &rarr;</a></div>"
    )
    return f'<div class="etfw">{"".join(blocks)}</div>{foot}{_WORKUP_CSS}'


def _is_tracked_etf(conn: sqlite3.Connection, ticker: str) -> bool:
    try:
        row = conn.execute(
            "SELECT instrument_type FROM tracked_companies WHERE UPPER(ticker) = ? LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0] or "").lower() == "etf"


def _chip(text: str, mono: bool = True) -> str:
    cls = "k-chip k-chip-mono" if mono else "k-chip"
    return f'<span class="{cls}">{escape(text)}</span>'


def _profile_block(conn: sqlite3.Connection, ticker: str) -> str:
    try:
        p = get_etf_profile(conn, ticker)
    except sqlite3.Error:
        p = None
    if p is None:
        return (
            "<div><h4>Profile</h4><p class='etfw-miss'>No profile on file — run "
            f"<code>python execution/fetch_etf_published_data.py --ticker {escape(ticker)}</code>"
            "</p></div>"
        )
    chips: list[str] = []
    if p.expense_ratio is not None:
        chips.append(_chip(f"ER {p.expense_ratio * 1e4:.0f}bp"))
    if p.aum_usd_m is not None:
        chips.append(_chip(f"AUM ${p.aum_usd_m:,.0f}M"))
    if p.pe_ratio is not None:
        chips.append(_chip(f"P/E {p.pe_ratio:.1f}"))
    if p.pb_ratio is not None:
        chips.append(_chip(f"P/B {p.pb_ratio:.1f}"))
    if p.distribution_yield is not None:
        chips.append(_chip(f"yield {p.distribution_yield * 100.0:.1f}%"))
    if p.inception_date is not None:
        chips.append(_chip(f"since {p.inception_date.year}"))
    ident = " · ".join(filter(None, (p.name, p.issuer, p.benchmark_index)))
    src_bits: list[str] = []
    if p.characteristics_source:
        as_of = p.characteristics_as_of.isoformat() if p.characteristics_as_of else "?"
        src_bits.append(f"characteristics: {p.characteristics_source} as of {as_of}")
    src = f'<div class="etfw-src">{escape(" · ".join(src_bits))}</div>' if src_bits else ""
    strip = "".join(chips) or "<span class='etfw-miss'>no characteristics on file</span>"
    return (
        f"<div><h4>Profile</h4><p>{escape(ident)}</p>"
        f'<div class="etfw-strip">{strip}</div>{src}</div>'
    )


def _loadings_block(repo_root: Path, ticker: str) -> str:
    loadings = read_materialized_etf_loadings(repo_root).get(ticker, [])
    if not loadings:
        return (
            "<div><h4>Style loadings</h4><p class='etfw-miss'>No qualifying style legs "
            "(thin history or r&sup2; below 0.10).</p></div>"
        )
    rows = "".join(
        f'<div class="etfw-row"><span>{escape(ld.key)}</span>'
        f'<span class="v">&beta; {ld.beta:+.2f} · r&sup2; {ld.r_squared:.2f} · n={ld.n_obs}</span></div>'
        for ld in loadings
    )
    return f"<div><h4>Style loadings</h4>{rows}</div>"


def _overlap_block(overlap: EtfOverlap | None, weights_present: bool) -> str:
    if overlap is None:
        why = (
            "weights cache empty"
            if not weights_present
            else "no holdings snapshot on file (N-PORT unresolved and no issuer overlay)"
        )
        return (
            f"<div><h4>Look-through</h4><p class='etfw-miss'>Unavailable — {escape(why)}.</p></div>"
        )
    head = (
        f"<p>{overlap.direct_overlap_pct * 100.0:.0f}% of the fund's weight is in names the "
        f"book already owns · {overlap.holdings_count} constituents · "
        f"{escape(overlap.source)} as of {overlap.as_of.isoformat()}</p>"
    )
    top = ""
    if overlap.top_overlaps:
        body = "".join(
            f"<tr><td>{escape(r.constituent)}</td>"
            f"<td class='num'>{r.etf_weight * 100.0:.1f}%</td>"
            f"<td class='num'>{r.book_weight * 100.0:.1f}%</td></tr>"
            for r in overlap.top_overlaps
        )
        top = (
            '<table class="etfw-table"><thead><tr><th>shared name</th><th>ETF w</th>'
            f"<th>book w</th></tr></thead><tbody>{body}</tbody></table>"
        )
    countries = ""
    if overlap.country_weights:
        ranked = sorted(overlap.country_weights.items(), key=lambda kv: kv[1], reverse=True)[:10]
        chips = "".join(_chip(f"{c} {w * 100.0:.0f}%") for c, w in ranked)
        countries = (
            f'<div class="etfw-strip">{chips}</div>'
            f'<div class="etfw-src">country exposure (intl {overlap.intl_weight * 100.0:.0f}% · '
            f"em {overlap.em_weight * 100.0:.0f}%)</div>"
        )
    return f"<div><h4>Look-through</h4>{head}{top}{countries}</div>"


def _whatif_block(repo_root: Path, ticker: str) -> str:
    rows = read_materialized_etf_whatif(repo_root).get(ticker, {})
    if not rows:
        return (
            "<div><h4>What-if</h4><p class='etfw-miss'>Not precomputed yet — runs with the "
            "morning Stage 0f (or <code>python execution/refresh_candidate_fit.py</code>).</p></div>"
        )

    def _num(row: dict[str, object], key: str, fmt: str, mult: float = 1.0) -> str:
        v = row.get(key)
        return format(float(v) * mult, fmt) if isinstance(v, (int, float)) else "—"

    body = ""
    for wkey in sorted(rows, key=float):
        r = rows[wkey]
        body += (
            f"<tr><td>{float(wkey) * 100.0:g}%</td>"
            f"<td class='num'>{_num(r, 'vol_before_ann', '.1f', 100)}% &rarr; "
            f"{_num(r, 'vol_after_ann', '.1f', 100)}%</td>"
            f"<td class='num'>{_num(r, 'sharpe_before', '+.3f')} &rarr; "
            f"{_num(r, 'sharpe_after', '+.3f')}</td>"
            f"<td class='num'>{_num(r, 'sharpe_delta_bps', '+.0f')}bp</td></tr>"
        )
    stamp = ""
    three = rows.get("0.03") or next(iter(rows.values()))
    through = three.get("prices_through")
    if isinstance(through, str):
        stamp = f'<div class="etfw-src">modeled book, pro-rata funded · prices through {escape(through)}</div>'
    return (
        "<div><h4>What-if</h4>"
        '<table class="etfw-table"><thead><tr><th>weight</th><th>vol</th><th>Sharpe</th>'
        f"<th>&Delta;SR</th></tr></thead><tbody>{body}</tbody></table>{stamp}</div>"
    )


def _role_block(db_path: Path, ticker: str) -> str:
    synthesis = read_role_synthesis(db_path, ticker)
    if synthesis is None:
        return (
            "<div><h4>Role in portfolio</h4><p class='etfw-miss'>Not generated yet — run "
            f"<code>python execution/build_etf_workup.py --ticker {escape(ticker)}</code> "
            "(one governed LLM call; cached until the workup inputs change).</p></div>"
        )
    verdict_cls = _VERDICT_TONES.get(synthesis.verdict, "k-pill")
    verdict = (
        f'<div class="etfw-verdict"><span class="{verdict_cls}">'
        f"{escape(synthesis.verdict.replace('_', ' '))}</span>"
        + (
            f" {_chip(f'suggested {synthesis.suggested_weight_band}')}"
            if synthesis.suggested_weight_band
            else ""
        )
        + "</div>"
    )
    adds = "".join(f"<li>{escape(a)}</li>" for a in synthesis.what_it_adds)
    adds_html = f"<ul>{adds}</ul>" if adds else ""
    caution = (
        f'<p class="etfw-miss">&#9888; {escape(synthesis.overlap_caution)}</p>'
        if synthesis.overlap_caution
        else ""
    )
    watch = "; ".join(synthesis.watch_items)
    watch_html = f'<div class="etfw-src">watch: {escape(watch)}</div>' if watch else ""
    stamp = synthesis_generated_at(db_path, ticker)
    stamp_html = f'<div class="etfw-src">generated {escape(stamp)}</div>' if stamp else ""
    return (
        f"<div><h4>Role in portfolio</h4>{verdict}"
        f"{render_prose(synthesis.role_summary)}{adds_html}{caution}{watch_html}{stamp_html}</div>"
    )

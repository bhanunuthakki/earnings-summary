"""§14 Synthesis — pulls cached lens artifacts for the workspace tab.

This builder is READ-ONLY. It does not invoke any LLM calls. The lenses are
generated separately via `python execution/run_lens.py --ticker X --all`
and cached in `llm_artifacts` (purpose='lens:<name>').

Build flow:
  1. Look up every lens registered in `synthesis_lenses.LENSES`.
  2. For each ticker-scoped lens, read the latest non-superseded artifact.
  3. Return a SynthesisSection with one SynthesisLensRow per cached lens.

When no lenses are cached, the section is OK with empty `lenses` — the
renderer shows a single "no synthesis cached" stub with the fix command.
This separates generation (LLM-driven, expensive, deliberate) from display
(cheap, always-on).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from report.models import SectionStatus, SynthesisLensRow, SynthesisSection

log = logging.getLogger(__name__)

# How old a lens artifact can be before we flag it as stale in the UI.
# Doesn't affect retrieval — purely informational.
STALE_THRESHOLD_DAYS = 21

# L12: an inline citation marker in a lens's prose. Citation resolution is
# gated on its presence so a lens that carries ``source_doc_ids`` only for
# lineage (no ``[n]`` in its memo) never pays for a doc lookup or risks a
# stray linkify — only a lens authored to cite (mgmt_credibility today) lights
# up. ``[n]`` is 1-2 digits, matching ``ui.cite_marks``.
_MARKER_RX = re.compile(r"\[\d{1,2}\]")


def _citations_for_source_docs(
    conn: sqlite3.Connection, source_doc_ids: list[int], *, ticker: str
) -> list[dict[str, object]]:
    """Resolve a lens's ORDERED ``source_doc_ids`` to ``ui.cite_marks`` chip
    payloads — the static-prose mirror of ``EvidenceItem.chip_payload``.

    The lens numbers its evidence ``[1]..[k]`` in the prompt from the SAME
    ordered list it stores as ``source_doc_ids``, so chip ``n`` (1-based
    position) resolves to ``source_doc_ids[n-1]``: the document backing the
    commitment the prose's ``[n]`` interprets. A slot whose document row is
    gone still emits a (link-less) chip so the numbering never shifts under the
    prose. Relative ``/source/<doc_id>`` hrefs match the fact-cell source chips
    (``ui.source_chip``) verbatim.

    Each chip carries the structured popover header (``ticker`` · ``doc_type``
    pill · ``period``) so the static-prose citation reads like the Ask fact
    chips. A document citation has no single ``value`` (it cites a whole filing,
    not one number), so the header IS the whole popover — ``label`` is left
    empty for a resolved doc (the header already says it) and falls back to
    "source document" only when the row is gone.
    """
    items: list[dict[str, object]] = []
    for i, doc_id in enumerate(source_doc_ids, start=1):
        doc_type: str | None = None
        period_end: str | None = None
        source_url: str | None = None
        try:
            row = conn.execute(
                "SELECT doc_type, period_end, source_url FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            doc_type = str(row[0]) if row[0] is not None else None
            period_end = str(row[1])[:10] if row[1] is not None else None
            source_url = str(row[2]) if row[2] is not None else None
        # Resolved docs let the header carry everything (empty label → no second
        # line); a vanished row keeps a generic label so the chip still names
        # something the header can't supply.
        label = "" if row is not None else "source document"
        items.append(
            {
                "n": i,
                "kind": doc_type,
                "label": label,
                "href": f"/source/{doc_id}",
                "source_url": source_url,
                "confidence": None,
                "ticker": ticker,
                "doc_type": doc_type,
                "period": period_end,
            }
        )
    return items


def build(ticker: str, repo_root: Path) -> SynthesisSection:
    ticker = ticker.upper()

    # Late import so the report layer doesn't depend on the synthesis_lenses
    # module unless the section is actually built.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    try:
        from llm_artifact_store import read_current  # type: ignore[import-not-found]
        from synthesis_lenses import list_lenses_for_ticker  # type: ignore[import-not-found]
    except ImportError as exc:
        log.warning({"event": "synthesis_imports_failed", "error": str(exc)})
        return SynthesisSection(status=SectionStatus.MISSING_DATA, ticker=ticker)

    db_path = repo_root / "data" / "portfolio.db"
    rows: list[SynthesisLensRow] = []
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(days=STALE_THRESHOLD_DAYS)
    # One short-lived connection, opened lazily only if a lens actually cites,
    # for resolving source_doc_ids → cite-mark chips (L12 static-prose citations).
    cite_conn: sqlite3.Connection | None = None
    try:
        for lens_name in list_lenses_for_ticker():
            art = read_current(
                ticker=ticker,
                purpose=f"lens:{lens_name}",
                scope="ticker",
                db_path=db_path,
            )
            if art is None or not art.content_md:
                continue
            generated_at = art.generated_at
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=UTC)
            # Citations resolve only when the memo carries inline [n] markers AND
            # the lens stored the ordered source docs they number against — i.e.
            # a lens authored to cite. Everything else renders exactly as before.
            citations: list[dict[str, object]] = []
            if art.source_doc_ids and _MARKER_RX.search(art.content_md):
                if cite_conn is None and db_path.exists():
                    cite_conn = sqlite3.connect(str(db_path))
                if cite_conn is not None:
                    citations = _citations_for_source_docs(
                        cite_conn, art.source_doc_ids, ticker=ticker
                    )
            rows.append(
                SynthesisLensRow(
                    name=lens_name,
                    content_md=art.content_md,
                    model=art.model,
                    generated_at=generated_at,
                    is_dirty=art.dirty,
                    dirty_reason=art.dirty_reason,
                    is_stale=generated_at < stale_cutoff,
                    citations=tuple(citations),
                )
            )
    finally:
        if cite_conn is not None:
            cite_conn.close()

    # Lenses are sorted by analytical importance (most-decision-critical first)
    # rather than alphabetically. Tweak to taste.
    _LENS_ORDER: dict[str, int] = {
        "five_min_reread": 0,
        "thesis_drift_qoq": 1,
        "bull_case": 2,
        "reverse_dcf": 3,
        "underweighted_facts": 4,
        "catalyst_calendar": 5,
        "filing_diff_narrative": 6,
        "footnote_anomaly": 7,
    }
    rows.sort(key=lambda r: _LENS_ORDER.get(r.name, 99))

    return SynthesisSection(
        status=SectionStatus.OK if rows else SectionStatus.MISSING_DATA,
        ticker=ticker,
        lenses=rows,
    )

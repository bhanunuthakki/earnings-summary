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
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from report.models import SectionStatus, SynthesisLensRow, SynthesisSection

log = logging.getLogger(__name__)

# How old a lens artifact can be before we flag it as stale in the UI.
# Doesn't affect retrieval — purely informational.
STALE_THRESHOLD_DAYS = 21


def build(ticker: str, repo_root: Path) -> SynthesisSection:
    ticker = ticker.upper()

    # Late import so the report layer doesn't depend on the synthesis_lenses
    # module unless the section is actually built.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    try:
        from llm_artifact_store import read_current  # type: ignore[import-not-found]
        from synthesis_lenses import list_lenses_for_ticker, LENSES  # type: ignore[import-not-found]
    except ImportError as exc:
        log.warning({"event": "synthesis_imports_failed", "error": str(exc)})
        return SynthesisSection(status=SectionStatus.MISSING_DATA, ticker=ticker)

    db_path = repo_root / "data" / "portfolio.db"
    rows: list[SynthesisLensRow] = []
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(days=STALE_THRESHOLD_DAYS)
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
        rows.append(
            SynthesisLensRow(
                name=lens_name,
                content_md=art.content_md,
                model=art.model,
                generated_at=generated_at,
                is_dirty=art.dirty,
                is_stale=generated_at < stale_cutoff,
            )
        )

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

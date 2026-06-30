"""Competitive-tracking instrumentation for the RBRK (Rubrik) micro-thesis.

The RBRK thesis is "the specialist (Rubrik) out-grows the scaled incumbent
(Cohesity)". Cohesity — post-Veritas — is now a funded, profitable rival
(~$1.5B ARR, ~$1.7B revenue, ~28% margin, ~19% data-resilience share,
NVIDIA-backed, targeting a 2026 IPO), so share gains must come against a peer,
not just legacy incumbents. To monitor that, this package plumbs three feeds
into the same canonical read path the rest of the platform uses
(``kpi_facts`` + the ``news`` table), so each competitive KPI in
``micro_thesis/holdings/RBRK.json`` reads a REAL stored value rather than a
hand-typed string:

  1. ``category_share`` — a manual-entry-friendly annual store of 3rd-party
     category position (Gartner Magic Quadrant / IDC data-protection share),
     ingested as annual (FY) ``kpi_facts`` via ``persist_manifest``.
  2. ``transcript_mentions`` — a deterministic extractor over RBRK earnings-call
     transcripts emitting per-quarter counts of (a) competitive displacement of
     legacy, (b) named >$1M / large-logo wins, (c) Cohesity/Veeam/Dell mentions.
  3. ``sec_watch`` — an EDGAR full-text-search watch that flags when Cohesity
     files its 2026 IPO S-1 (the data-unlock for a real RBRK-vs-Cohesity
     net-new-ARR-share metric), wired into the additive news fetch.

Two name layers, both single-source-of-truth here:

  * The GRANULAR ``kpi_facts`` definition names the loaders write (one per
    sub-metric), e.g. the Gartner MQ ordinal or each transcript mention count.
  * The OWNER's composite tier-2 KPI names already in ``holdings/RBRK.json`` —
    the ones the owner's spec declares. ``holdings_sync`` composes the granular
    facts (and the S-1 watch state) into THESE KPIs' ``current`` field, so the
    instrumentation feeds the owner's existing KPIs rather than inventing new
    ones.
"""

from __future__ import annotations

from typing import Final

# --- GRANULAR kpi_facts definition names (what the loaders write) ----------- #
# Annual category-share metrics (piece 1).
KPI_CATEGORY_SHARE_RBRK: Final = "Data-protection category share — Rubrik (%)"
KPI_CATEGORY_SHARE_COHESITY: Final = "Data-protection category share — Cohesity (%)"
# Gartner Magic-Quadrant position as a chartable ordinal (Niche=1, Visionary=2,
# Challenger=3, Leader=4); the prose label travels in the fact's source_excerpt.
KPI_GARTNER_MQ_ORDINAL_RBRK: Final = "Gartner MQ position — Rubrik (ordinal 1-4)"
# Per-quarter transcript competitive-mention counts (piece 2).
KPI_MENTIONS_DISPLACEMENT: Final = "Competitive displacement-of-legacy mentions (count)"
KPI_MENTIONS_LARGE_WIN: Final = "Large-deal / >$1M-logo win mentions (count)"
KPI_MENTIONS_NAMED_COMPETITOR: Final = "Named-competitor mentions — Cohesity/Veeam/Dell (count)"

# --- OWNER composite tier-2 KPI names (already in RBRK.json) ----------------- #
# These are the KPIs the owner's competitive spec declares; holdings_sync writes
# their ``current`` field from the granular facts above + the S-1 watch state.
OWNER_KPI_CATEGORY_SHARE: Final = (
    "Category share - Gartner MQ position / IDC data-protection market share (annual)"
)
OWNER_KPI_MENTIONS: Final = "Competitive-displacement + large-logo win mentions per earnings call"
OWNER_KPI_INCREMENTAL: Final = (
    "RBRK net-new ARR / (RBRK + Cohesity net-new ARR) - 'who wins the incremental'"
)
# The legacy phrasing of the incremental-share KPI (also S-1-unlocked).
OWNER_KPI_INCREMENTAL_LEGACY: Final = (
    "Rubrik net-new ARR / Cohesity net-new ARR (competitive share of incremental)"
)

# The owner tier-2 KPIs holdings_sync mirrors stored values into.
SYNCED_KPI_NAMES: Final[tuple[str, ...]] = (
    OWNER_KPI_CATEGORY_SHARE,
    OWNER_KPI_MENTIONS,
    OWNER_KPI_INCREMENTAL,
    OWNER_KPI_INCREMENTAL_LEGACY,
)

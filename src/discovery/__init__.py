"""Discovery pipelines (master build P5.3) — surface new names, never build.

  screens.run_screens      — factor screens over the index-member universe
                             (local FMP caches only)
  adjacency.mine_adjacency — companies repeatedly named near the portfolio
                             (watchlists / transcripts / news)
  store                    — discovery_candidates CRUD (0081); status is
                             owner-driven via the P5.4 queue

Candidates land in an approval queue; nothing here triggers an eval build
(the directive's "Discovery: queue, never auto-build").
"""

from discovery.adjacency import AdjacencyHit, mine_adjacency
from discovery.screens import ScreenHit, run_screens
from discovery.store import (
    CANDIDATE_STATUSES,
    CandidateRow,
    get_candidate,
    list_candidates,
    set_status,
    upsert_candidate,
)

__all__ = [
    "CANDIDATE_STATUSES",
    "AdjacencyHit",
    "CandidateRow",
    "ScreenHit",
    "get_candidate",
    "list_candidates",
    "mine_adjacency",
    "run_screens",
    "set_status",
    "upsert_candidate",
]

"""On My Mind — the reverse-chron capture feed (the front of the Thought Partner
funnel).

A read model over ``analyst_notes`` where ``source='capture'`` (musings +
readings), keyset-paginated, carrying the action ladder
**dismiss · save-for-later · discuss · incorporate-into-research**. It ABSORBS the
Wondering flag as an inline badge (a note that produced a ``research_tasks`` row)
and reuses the research loop as its "incorporate" rung. Transient working memory
that feeds the durable Worldview; storage is the last step, never the product.

Behind ``LEDGER_ONMYMIND`` (default off) until the surface is wired end-to-end.
"""

from __future__ import annotations

from onmymind.feed import (
    LADDER_LABELS,
    LADDER_VERBS,
    ActionResult,
    FeedItem,
    FeedPage,
    act_on_feed_item,
    load_feed,
    onmymind_enabled,
)

__all__ = [
    "LADDER_LABELS",
    "LADDER_VERBS",
    "ActionResult",
    "FeedItem",
    "FeedPage",
    "act_on_feed_item",
    "load_feed",
    "onmymind_enabled",
]

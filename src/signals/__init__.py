"""The information-diet signal substrate (alembic 0095).

`signals` is the canonical typed store for TRACKED-name informational signals —
the inverse product of the thesis-breach alerter. Where the inbox is a decaying
PUSH lane ("what needs your action"), the diet panel is a non-decaying PULL lane
("what a diligent analyst should ingest"): sell-side rating changes, general
news, forward-dated investor days, and the disclosed fast-follow scaffolds.

The split is structural, not cosmetic: a `signals` DIET row is NEVER converted
into an `InboxItem` and so NEVER enters the urgency-decay scorer or the
materiality veto built to suppress non-thesis news (design_language
"Diet-vs-alert"; guard `tests/test_signals_diet_guard.py`). The two NEWS-mirrored
types (`general_news`, `consensus_rating`) bridge to the alert lane only via the
EXISTING `news` → `material_news` → alert pipeline — the signal row itself stays
in the diet lane.

Sibling boundary: this package owns `signals` (tracked names). The discovery
package owns `discovery_candidates`/`discovery_signals` (untracked candidate
sourcing) — they do not share a table.
"""

from __future__ import annotations

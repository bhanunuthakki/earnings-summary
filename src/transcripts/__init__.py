"""P4 transcript longitudinal tracking (docs/design/disclosure_change_build_stack.md P4).

`longitudinal.py` — deterministic turn parsing, speaker-role classification,
Q&A pairing, KPI-mention presence, roster diffing, and the disclosure_events
writer. `transcript_judgment.py` — the two LLM judgment calls (non-answer
classification + tone scoring; KPI-relevance triage), each batched per
call/ticker, never per question.
"""

from __future__ import annotations

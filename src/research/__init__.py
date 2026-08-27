"""The Ledger Phase-1 conversational research loop.

An owner-authored musing is classified by ``capture_intent`` and, when it is a
research question, becomes an INERT research proposal the owner reviews
(approve / research-further / steer / reject) in both the web inbox and the
Telegram thread. Every output is a proposal row + a one-tap affordance — nothing
writes live until approved, and the research pass is SEMI-AUTO (classification
automatic, running one tap).
"""

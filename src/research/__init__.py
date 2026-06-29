"""The Ledger Phase-1 conversational research loop.

A wondering detected in an owner-authored musing becomes an INERT research
proposal the owner reviews (approve / research-further / steer / reject) in both
the web inbox and the Telegram thread. Every output is a proposal row + a one-tap
affordance — nothing writes live until approved, and the research pass is
SEMI-AUTO (detection automatic, running one tap).

Modules:
  detect  — wondering_detect: regex trust-zone pre-gate + governed classifier.
"""

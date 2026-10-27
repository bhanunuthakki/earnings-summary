"""Shared text and reading capture for direct agent sessions and the web UI.

Turns a typed musing into a durable, ticker-linked ``analyst_notes`` row
(``kind='musing'``, ``source='capture'``). The raw words are staged transiently
in ``raw_capture_sessions`` (0116) and purged once the structured musing exists;
a durable summary-level trail lives in ``capture_audit_log`` (0117).

Modules:
  audit       — the summary-level audit-log writer (0116).
  matcher     — deterministic roster-alias ticker matching (no LLM).
  scrub       — light deterministic PII redaction before any LLM hop.
  ingest      — deterministic text and reading landing.
"""

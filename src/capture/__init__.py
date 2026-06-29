"""The Ledger capture subsystem.

Turns a stream-of-consciousness musing — spoken or typed, arriving via Telegram,
Gmail, or the desktop tray — into a durable, ticker-linked ``analyst_notes`` row
(``kind='musing'``, ``source='capture'``). The raw words are staged transiently
in ``raw_capture_sessions`` (0115) and purged once the structured musing exists;
a durable summary-level trail lives in ``capture_audit_log`` (0116).

Modules:
  audit       — the summary-level audit-log writer (0116).
  matcher     — deterministic roster-alias ticker matching (no LLM).
  scrub       — light deterministic PII redaction before any LLM hop.
  transcribe  — local faster-whisper voice→text (no sidecar; ffmpeg-backed).
  telegram    — the Telegram Bot API client (long-poll, voice, inline keyboards).
  ingest      — the transcribe→scrub→structure→match→land pipeline.
"""

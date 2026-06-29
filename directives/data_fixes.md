# Data fixes — single-ticker data-correction inbox

Auto-populated, append-only backlog of **single-ticker data corrections** and
un-routable comments parked for human disposition. The comment processor writes
here via the `fix_data` and `needs_triage` routes
(`execution/process_report_comments.py`, append-mode — the file is recreated if
absent). Each line is one open item; tick the box when disposed.

This is deliberately **separate** from the other two backlogs — don't merge them:

- `platform_backlog.md` — cross-workspace renderer/pipeline **changes** (the
  `platform_change` route), the canonical feature/bug tracker.
- `residual_backlog_2026_06.md` — a **historical** point-in-time snapshot, not a
  live to-do list.

---

- [ ] **AMZN** (free_text · `-1469.5%`) — reported 2026-05-22: Looks wrong

# Claude-session bridge (B8, 2026-07-19 program overhaul, Workstream B)

**Why this exists.** Research home splits by depth (owner ruling #9): thin
in-app Ask for quick pulls, deep research in a Claude Code session for
anything that needs a real pass — filings, cross-quarter threads, thesis
work. Both feed the SAME distillation tap (B4's daily 18:00
`session_distill` sweep) so a belief formed in a Claude session and a belief
formed in Ask end up in the one Worldview, not two disconnected corpora.
This is the contract between the platform and a Claude session: what each
side owes the other.

## What the platform promises a session

A fresh context pack, on demand, for free:

```
python execution/session_context_pack.py
```

- **Zero LLM calls.** Pure SQLite reads. Safe to run at any hour, at any
  frequency, with zero quota interaction — it never touches `llm_budgets`
  or the `claude` CLI subprocess.
- **Always renders.** Every section degrades to an explicit empty-state
  note on a missing table, column, or DB rather than raising. A DB that
  predates half the features this pack reads from (e.g. no
  `session_prompt` column yet, no `v_decision_journal` view) still prints a
  usable pack — the section just says so honestly instead of crashing the
  session's opening move.
- **The fields**: current Worldview (Tenets + Stances + Themes), open
  questions/wonderings (capped, wondering-flagged), open owner decisions
  with their falsifiers (ungraded only), any `research_tasks` rows carrying
  a `session_prompt` (queued specifically for a Claude session — B7), the
  owner-profile anchor (affirmed capacity/appetite facts), and a header
  stating the machine timestamp plus DB freshness (max `updated_at` across
  every source read) so a stale pack is visible rather than silently
  trusted.

The pack is READ-ONLY and idempotent — running it twice in a row changes
nothing and costs nothing.

## What a session owes back

Land every durable outcome through `execution/land_session_notes.py`, one
invocation per outcome — never write to `data/portfolio.db` directly:

| Kind | When | Example |
|---|---|---|
| `musing` | a thought worth keeping | `land_session_notes.py musing --text "..."` |
| `close-intent` | the session resolved a standing intent | `land_session_notes.py close-intent --ref <ref> --verdict <v> --reason "..."` |
| `decision` | a position decision was stated | `land_session_notes.py decision --ticker T --direction buy --conviction high --falsifier "..."` |
| `transcript` | ALWAYS, at session end | `land_session_notes.py transcript --file <path> --session-ref <id>` |

The `transcript` kind is the safety net — it bridges the whole session into
`raw_capture_sessions` (`channel='claude_session'`) so the distillation
sweep can find anything belief-shaped that wasn't called out via a discrete
kind above (a revised Tenet, a resolved open question, a stance shift).
Land it every time, even when nothing else seemed durable enough for its
own command.

Landing is LLM-free by design — none of the four kinds fires an LLM. That
is the whole point: a session can land its outcomes durably before any
fallible or quota-metered step, the same invariant every other capture
channel (Telegram, the coach, the poller) already follows.

## What happens after landing (B4, not this session's job)

The daily 18:00 `session_distill` sweep (`execution/run_session_distill.py`)
reads landed transcripts and idle Ask threads, runs the ONE LLM pass per
session, and auto-adopts grounded outcomes (new/revised Tenets, resolved
questions, stance revisions) with an owner-facing Revert path
(`synthesis.tenets.revert_tenet`) — never a silent, unrecoverable write. A
session never distills its own transcript; it only lands the raw material.

## Quota rule

Interactive Claude Code sessions are bursts under the global ≥6–7h spacing
rule (`directives/llm_quota_scheduling.md`) and share ONE subscription quota
with the scheduled fleet. A session that fired its own distillation LLM
call at close would be exactly the kind of ad-hoc burn that rule exists to
prevent, on top of duplicating the 18:00 sweep's job. `land_session_notes.py`
enforces this structurally (it has no LLM-firing code path at all, not just
a convention) — the constraint lives in the tool, not in discipline.

## See also

- `.claude/skills/research-session/SKILL.md` — the skill that drives this
  contract inside a session (start-of-session pack read, end-of-session
  landing, the exact CLI invocations).
- `execution/session_context_pack.py` — the read half (this doc's "what the
  platform promises").
- `execution/land_session_notes.py` — the write half (this doc's "what a
  session owes back").
- `directives/llm_quota_scheduling.md` — the quota registry `session_distill`
  rides (daily 18:00).

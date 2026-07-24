---
name: research-session
description: Run a deep-research Claude Code session against the earnings-summary platform bridge — read the current context pack before researching, land every outcome back through the durable capture pipeline at the end. Triggers include "start a research session", "research session on <ticker/topic>", "/research-session", "deep-dive on <ticker>", "let's research whether...".
---

# research-session

The platform bridge for deep research (owner ruling #9, 2026-07-19 program
review): thin in-app Ask handles quick pulls; anything that needs a real
research pass — reading filings, following a thread across quarters,
working out a thesis — happens in a Claude Code session like this one. The
bridge has two halves: `execution/session_context_pack.py` (read, what the
platform already knows) and `execution/land_session_notes.py` (write, what
you're handing back). Full contract: `directives/claude_session_bridge.md`.

## 1. At session START — read the context pack

Run this FIRST, before any research, and read the output in full:

```
python execution/session_context_pack.py
```

Zero-LLM, safe at any hour. It prints the owner's current Worldview
(Tenets/Stances/Themes), open questions and wonderings, open owner decisions
with their falsifiers, and any research-task prompt blocks already queued
for a Claude session. Don't re-derive a belief the pack already states, and
don't contradict a standing Tenet or falsifier without addressing it
explicitly.

If a section shows a `session_prompt` block (a task the owner queued
specifically for a Claude session), treat that as the working brief unless
the owner's live message overrides it.

To write the pack to a file instead of stdout (e.g. to keep it open in a
tab while you work): `python execution/session_context_pack.py --out
.tmp/session_pack.md`.

## 2. Do the research

Whatever the session's actual work is — filings, transcripts, web research,
model-building. This skill doesn't constrain that part.

## 3. At session END — land every discrete outcome

For each durable outcome the session produced, run ONE
`land_session_notes.py` invocation — don't batch multiple outcomes into one
call:

```
# a musing / thought worth keeping
python execution/land_session_notes.py musing --text "..." [--session-ref <id>]

# close a standing intent the session resolved
python execution/land_session_notes.py close-intent --ref seed:intent:leap-sleeve \
    --verdict resolved-rejected --reason "..." --session-ref <id>

# a position decision stated in the session
python execution/land_session_notes.py decision --ticker NVDA --direction buy \
    --conviction high --falsifier "..." --size-usd 31000
```

Then, ALWAYS, land the full session transcript for later distillation:

```
python execution/land_session_notes.py transcript --file <path> --session-ref <id>
# or via stdin:
python execution/land_session_notes.py transcript --session-ref <id> < transcript.txt
```

The discrete-outcome commands (musing/close-intent/decision) capture things
you already know are durable; the `transcript` kind is the safety net that
lets the sweep find anything you didn't call out explicitly (a revised
Tenet, a resolved open question, a stance shift).

## What NOT to do

**Do not run any distillation LLM call at session end.** No summarizing the
session into a Tenet, no self-grading, no calling an LLM to decide what
mattered. Landing is LLM-free by design (`land_session_notes.py` never fires
an LLM) — the daily 18:00 `session_distill` sweep owns distillation, reading
what you landed. This is a quota-discipline rule, not a laziness shortcut:
interactive sessions are bursts subject to the ≥6–7h spacing rule
(`directives/llm_quota_scheduling.md`), and a session-end distillation call
is exactly the kind of ad-hoc LLM burn that rule exists to prevent.

Do not write to `data/portfolio.db` directly. Every write goes through
`land_session_notes.py`.

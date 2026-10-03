---
name: earnings-summary-investing
description: Use the earnings-summary project's existing sources and tools for public-company and ETF research, earnings previews and readouts, thesis checks, valuation, investment decision cards, evaluation-list review, portfolio risk, and investment memos. Prefer this workflow for research in earnings-summary and its portfolio or evaluation names. Route next-dollar allocation to the existing next-dollar-allocation skill. Does not execute trades or authorize publication.
---

# Earnings-summary investing

Help the owner decide what changed, whether it changes the investment case, and
what evidence supports the conclusion. Use this project's workflow directly.
Do not load the generic Public Equity Investing plugin as a routine dependency.

## Resolve the task and authority

Resolve this file's real path; the installed skill can be a symlink. Its repository
is four parents above the skill directory. Read the repository's current
`AGENTS.md`. Use [routes](references/routes.md) for the smallest task owner.
Read `directives/directive_manifest.json` before treating a directive as policy:
canonical files own policy, runbooks supply mechanics, drafts remain proposals.

Carry forward the owner's named scope, current preferences and authorization.
Read the canonical roster for portfolio and evaluation membership and levels.
Do not infer the roster from thesis files, a static ticker list or a prior brief.
For allocation, use the full account book through `next-dollar-allocation`.

Before live access, follow `directives/agent_host_operations.md` and the machine
operations procedure. Resolve the configured host and database. Use an explicit
approved database path for database commands. A development checkout is not a
live database, fallback host or place for duplicate background services.

## Establish the evidence

Start with `micro_thesis/holdings/<TICKER>.json`, canonical database evidence,
`transcripts/`, `ir_documents/` and existing research artifacts. Thesis thresholds
and owner assumptions come from their recorded authority. A draft or stub does
not become approved because it is present. Use issuer filings and investor
relations sources for missing company evidence through the typed acquisition
and intake routes. Generic search can discover a source; it cannot replace it.

Resolve issuer identity and the exact fiscal year, quarter, period end and release
date for every earnings request. A fiscal Q2 can precede another issuer's Q2 by
months. Preserve an explicitly requested quarter. For an already reported Q3,
label its preview as historical and enforce the historical knowledge cutoff;
changing it to a current readout requires a scope decision. A future quarter
cannot have a post-earnings readout. Mark earnings dates as confirmed, estimated
or unannounced. Check
currency, unit, scope, accounting basis, segment changes and comparative recasts.

Use the shared provenance-aware readers. Preserve source wording and raw bytes.
Separate acquisition completeness from extraction completeness. A URL, captured
document or generated report alone does not establish complete evidence. Report
missing packages, sections, facts and rejected semantic admissions explicitly.
Call an output decision-grade only when source authority, completeness, semantic
admission, reader parity and reconstruction checks all pass under project policy.

## Form the investor judgment

For an earnings preview, state the expectation bar, evidence that would confirm
or weaken the thesis, key questions and the likely decision after each outcome.
For a readout, compare reported results with dated prior guidance and available
expectations. Explain what changed in the business and in the investment case.
Do not invent consensus, call a beat without a comparable expectation, or confuse
a guidance change with a change in the underlying business.

Connect the evidence to the approved thesis and valuation assumptions. Identify
the strongest counter-case and the evidence that would prove or kill the thesis.
Use code or existing model calculations for arithmetic and sensitivities. Show
which assumption can reverse the conclusion and the next event to watch. Make
the supported action clear: research further, watch, maintain or reconsider the
investment case. Position sizing uses recorded intent and portfolio context.

Distinguish reported facts, management claims, consensus estimates, calculations
and analyst inference. Missing evidence limits the conclusion; it does not imply
that the thesis passed or that the owner should hold cash. Unsupported modes can
produce bounded source-backed research without inventing a new durable pipeline.

## Deliver through the existing surface

Use the route's existing schema and report family. Keep a quick answer concise;
do not impose a model, deck, memo or intake form on every question. Save durable
research in the configured private output authority. Repository-generated
deliverables use `output/research/<TICKER>/`; `.tmp/` holds intermediate receipts.
Retain a source/context manifest or claim-level citations with the output.

Check the route's actual scope before a bulk run. The pre-earnings CLI filters an
eligible roster and date window; `--ticker` does not opt in an evaluation name.
The post-earnings CLI covers portfolio names; explicit evaluation generation uses
the supported cockpit route. Generation can spend LLM quota and write artifacts.
Use existing cache and resumption rules rather than forcing unchanged reruns.

Report generated, validated, committed, deployed and live-verified separately.
Check the live runtime revision and selected artifact identity when verifying
the front end; a local commit does not prove the live host runs that revision.
Keep private availability distinct from public disclosure. Publication requires
authorization for the concrete artifact, destination and disclosure effect.
Do not execute trades or change owner constraints from an analyst recommendation.

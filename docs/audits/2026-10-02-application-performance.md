# Application usability and performance review

## Scope and cause

This review covers the eight Work OS destinations, their panel services, legacy panel reads, the standalone report input read, shared cache coordination, and tracker analytics. It does not change financial admission, provenance, write approval, or production host authority.

The measured problem is avoidable page construction and weak request recovery. Other chats do not exclusively own the read services. Identical concurrent panel requests share a cache build, but the previous wait had no deadline. Explore constructed a full metric catalog before showing its initial controls. Composite pages constructed independent sections in one response. The browser's generic deadline could expire before serial tracker requests finished.

## Live baseline

Measurements used the configured private Windows route. These are individual observations, not percentile estimates.

| Read | Observed duration | Interpretation |
| --- | ---: | --- |
| Windows loopback health | 0.007 s | Backend responds promptly |
| Mac to canonical private health | 0.084 s | Private route adds a small amount of delay |
| Explore, NU, cold | 13.561 s | First byte at 13.543 s; TLS setup at 0.053 s |
| Explore, same request, warm | 0.096 s | Cache substantially changes the result |
| Decision Audit Log, warm | 0.16 s | Read was available during diagnosis |
| Performance | 2.04 s | Analytics add material server work |

The cold Explore result identifies server work as the main delay in that request. It does not prove that every slow request has the same cause.

## Coverage and implemented behavior

| Surface or service family | Correction |
| --- | --- |
| Portfolio Cockpit | Shared bounded reads; failed refresh preserves records; explicit retry |
| Performance & Risk | Early shell; independent analytics regions; visible-region concurrency limited to two; risk tabs cancel obsolete reads |
| Company Desk | Shared bounded reads; superseded company requests cannot replace newer results |
| Evaluation | Bounded hydration with retry; saved records remain available on refresh failure |
| Brief Library and reader | Filter and reader reads cancel on replacement or close; late results are rejected; lookup failures retain the selected company |
| Explore | No initial full catalog; catalog loads when Work Bench opens; relevant KPI identity queries; bounded deterministic analysis; previous analysis is explicitly marked when selections change or refresh fails |
| Decision Audit Log | Early shell; independently loaded read, decisions, research items, memos, and triggers; one failed section does not block the rest |
| Operations | Shared bounded reads; failed README status keeps Apply disabled until status is verified |
| Legacy panels | Shared read transport for Journal, Discovery, Ledger, Worldview, memos, allocation decisions, evaluations, lifecycle, source calls, provenance, ticker controls, cron health, and copilot history; final scan also includes triage, Red Team, and portfolio reloads |
| Standalone report | Bounded DCF input read using its configured server origin; explicit retry; report mutations retain their existing owners |
| Saved Copilot analysis | A 30-second deadline covers restored analysis and its body; replacement or close cancels reads; retry restores only the saved analysis |
| Tracker analytics | One total time allowance across sequential reads; no successful partial pagination after expiry |
| Panel response cache | Bounded duplicate-request wait; explicit Busy response; invalidation releases waiters and rejects obsolete completions |

Read failures are not successful empty results. Writes and LLM event streams retain their existing replay and lifecycle contracts. No automatic mutation retry is introduced.

## Time allowances and diagnostics

- General same-origin browser reads: 15 seconds, including the complete response body.
- Independent analytics sections: 45 seconds, with loading status and Retry.
- Deterministic Explore analysis: 30 seconds, with cancellation and previous-result preservation.
- Saved Copilot analysis restoration: 30 seconds, including the complete response body.
- Legacy tracker activation polling: 90 seconds total; each read is at most 45 seconds; Retry refreshes status without repeating activation.
- Tracker foreground analytics: a shared 30-second default allowance; an explicit larger caller allowance remains supported.
- Duplicate panel-cache waits: 1 second, followed by HTTP 503 and Retry-After.
- Composite sections: at most two concurrent visible-region reads per console.

These are failure limits, not desired load times. The desired behavior is an early usable interface and independently progressing data regions. Server-Timing separates cache state and request duration. Browser read measurements omit URLs and private payloads.

## Evidence

Hermetic regressions exercise stalled bodies, aborted input, safe HTTP errors, cache ownership, bounded cache wait, invalidation races, total tracker allowances, late filter responses, retry, lazy route dispatch, and cancellation. Explore regressions also cover deferred catalog access, reversed responses, closed Work Bench, analysis expiry, saved-view failure, and previous-result preservation.

Before/after browser evidence covers Explore, Decision Audit Log, and Performance at 1440×1000 and 900×900. A controlled fixture delays each data dependency by 1.2 seconds. The previous client waits about 1.21–1.22 seconds for controls; the revised client exposes controls in about 7–18 milliseconds while data regions remain loading. This demonstrates removal of the blocking dependency in that fixture. It is not a live Windows speedup measurement.

Audit failure/retry and navigation cancellation pass at both widths. The observed maximum section concurrency is two. No JavaScript errors or horizontal overflow were observed. All browser contexts were closed. No development application server or listener was started.

Local receipts and screenshots are in `.tmp/service-performance-browser-20261002/`. The KPI query benchmark is in `.tmp/explore-performance-20261002/receipt.json`; it uses synthetic records and preserves exact output parity.

Standalone DCF before/after evidence at 1280 and 960 pixels is in `.tmp/performance-report-dcf/`. Report golden expectations were reviewed for the intended script and retry-control changes, then checked in comparison mode. Legacy browser regressions verify that failed Red Team, triage, and risk reads retain content, and that Retry does not repeat the preceding write. A portfolio window regression verifies chart and date preservation followed by recovery.

## Instruction changes

The canonical global contract now treats usability and performance as product behavior. The frontend procedure requires task-specific budgets, progressive loading, truthful degraded states, preserved inputs/results, and cold/warm plus slow/failure evidence. The code-change procedure requires compatible request deadlines, bounded work, cancellation, cache ownership, and diagnostics. Generated runtime instructions were refreshed from the canonical source.

## Final validation

The broad test matrix completed with 17,379 passing tests, 68 skips, and ten failures. Each failure was investigated and corrected. A combined rerun of all ten failed cases passes. The full matrix was not repeated after those corrections.

The final changed-file checks pass formatting, lint, strict typing with zero diagnostics, suppression controls, and architecture boundaries. The whole-project static-quality gate also passes, with lower diagnostic and suppression ceilings. All 37 computed browser canaries pass. The design synchronization gate passes. All 46 report golden comparisons pass after review of the intended expectations. Focused request-lifecycle, cache, tracker, query parity, and canonical-reader regressions also pass. Generated instruction stubs pass the read-only synchronization check with no content drift.

The isolated release dependency review retains all 171 reviewed production dispositions, with zero unknown or unresolved production edges. Its 121 test residuals include nine explicit synthetic Node harness calls. The scanner and production dependency targets are unchanged. The quality ceilings were tightened to the measured counts; no suppression allowance was increased.

At review time, the changes remain local and uncommitted. No production database was changed. No Windows application was replaced. Live after-change timing remains unverified.

The isolated release passes 223 integration cases and 519 closure/regression cases. Its exact quality baseline is 2,632 retained Python files, 2,800 Pyright diagnostics, and 2,231 suppressions. Changed-file strict typing reports zero diagnostics. Sol 6.1 independently reviewed the frozen application files and returned PASS. Release, deployment, and live backtest receipts supersede the review-time status when those steps complete.

## Remaining verification boundary

The revised application is a local candidate. Live Windows after-change timing and behavior require deployment of a concrete reviewed version through the canonical host procedure. Browser cancellation cannot stop a synchronous server calculation already running. Cache coordination and tracker allowances bound the relevant waiting and subsequent work. The Python requests read timeout measures socket inactivity; a continuously trickling response can exceed the nominal wall-clock allowance before returning. Late results are rejected and subsequent reads stop, but a hard transport deadline is not claimed.

No production data write or application replacement is authorized by this review alone.

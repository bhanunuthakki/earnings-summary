# Pipeline reliability and inherited work

This is an implementation handoff, not a runtime health certification. Git `main`
is the executable source baseline. Only canonical directives own policy; pending
branches, draft research models, quality receipts and `.tmp/` artifacts do not
supersede it. Production database and background services belong to the configured
Windows authority, never a development checkout.

## October 1, 2026 reconciliation

The reliability work starts at `0414be406a1278191d84313450d5be94714962b9`, which
includes merged PR1591 and its retirement of scalar Score/Fit compatibility.
It lives on `codex/pipeline-evidence-reliability`, isolated from Antigravity's
local work and retained Windows scratch changes. Antigravity reported a local
stopping point; its handoff confirms the same merged base and separate pending
work. The untracked historical status report was preserved with its hash in the
ignored reconciliation inventory, leaving the shared checkout clean. Existing branch references and dirty
checkouts are preserved. The main checkout had no inherited code diff to move.
Unmerged work already has separate branch references; none was discarded.

| Retained work | Disposition |
| --- | --- |
| PR1590, exact-subject quality evidence | Pending reconciliation. Receipts name an older source subject; they cannot certify this candidate. |
| PR1571, verified retained-evidence replicas | Pending reconciliation and native qualification. Preserve its recovery work; do not introduce an independent replica fallback here. |
| PR1445, visual conformance | Separate visual work, substantially behind main. Requires its own rendered review. |
| Older merged worktrees | Historical checkouts, not source authority. Commit ancestry alone does not permit deletion of ignored artifacts or active resources. |
| Dirty or unknown worktrees | Preserve until ownership and recoverability are resolved. |

The private, ignored reconnaissance receipt includes the complete worktree/ref
inventory and bounded status observations. It found 239 registered worktrees,
274 branches, two dirty checkouts and 34 invalid/missing registrations. These
counts describe the initial inventory. After Antigravity's stopping-point handoff,
34 invalid Git registrations were pruned only after their metadata was backed up.
All branch refs were verified unchanged; no physical worktree directory was deleted.
Unmerged and dirty work remains preserved for separate reconciliation.
Subsequent sessions should compare their exact Git top-level, branch, HEAD and
current diff before editing. Do not revive work from an old checkout merely
because its directory is named clean or live.

## Evidence and refresh behavior

Price time, model-run time, source capture time and financial period are separate.
`inputs_as_of` remains a reproducibility cutoff; it is not proof that every
financial input is current. File modification time never proves issuer publication,
semantic admission, complete extraction, or an owner's assumption review.

The shared read-only valuation readiness projection is used by allocation and
advisor comparison gates. Historical valuations remain visible. Missing or
unverified financial completeness blocks preferring a stock from those values and
reports stable reason codes. Fresh quotes and unrelated newly admitted facts
cannot upgrade the evidence used by an older model. Current supported lineage does
not yet establish complete issuer coverage for every valuation family: the gate
must report that limitation rather than invent a complete receipt.

Inspect one persisted model before a recommendation:

```sh
python execution/sqlite_bootstrap.py execution/valuation_preflight.py \
  --db-path "$EARNINGS_SUMMARY_DB_PATH" --ticker MELI
```

Exit 0 means the projection passed its requirements; 2 means blocked evidence;
3 means the database authority is unavailable. This command performs no fetch,
LLM call, model write or automatic recovery. Use its exact reasons to select the
owning acquisition, extraction, admission or valuation workflow. Do not replace
missing issuer evidence with generic web research.

Stale refresh planning requires successful, nonempty, recent receipts for all six
annual/quarterly income, balance-sheet and cash-flow endpoints. A recent profile,
failed receipt, absent endpoint or future timestamp cannot suppress acquisition.
The legacy quarterly-only dispatcher fetcher does not itself establish those six
receipts; the canonical receipt writer remains `execution/save_fmp_data.py`.
Independent refresh steps continue after failure, with a nonzero aggregate result.
The artifact drain distinguishes an unavailable queue from a checked empty queue;
only a checked empty queue may be reported as idle. Manifest-only and unmapped
queues retain their existing behavior. An empty artifact queue does not establish
overall pipeline freshness.

## Recovery boundaries from the live audit

These are observed causes, not a declaration that current production is repaired:

- Artifact children omitted the parent's database authority. The code fix passes
  that authority explicitly and handles canonical-target junction equivalence
  without accepting an ordinary checkout-local database.
- Scheduled LLM setup lacked the fleet policy location and explicit route. Repair
  the host's canonical `AGENT_INSTRUCTIONS_HOME` or approved route injection; do not
  duplicate model/provider priority in application code. Validate the resolver
  before resuming the exact failed stage.
- FMP authentication/payment availability remains an operator/provider issue.
  A recorded auth-missing circuit and HTTP402 are distinct evidence; neither proves
  whether a subscription is currently paid. Restore the approved credential/service
  and probe through the existing circuit recovery contract, without retry storms.
- Legacy SEC accession identities were quarantined; semantic validation and
  same-document correction conflicts remain failed admission, not empty datasets.
- The derived-metrics fix normalizes reporting dates before grouping and sorting,
  preserving the issuer's local fiscal date and source ranking. Partial Tracker
  risk payloads and artifact `no_progress` still require their owning diagnostics.
- Disabled acquisition lanes have intentional hold dispositions. Do not enable
  them as an incidental effect of a code fix. Interrupted Scheduler results and
  logs ending mid-run do not establish successful source coverage.

Deploy only a validated candidate after reconciling active work. Recheck runtime
source identity, route setup, schema compatibility and exact failed checkpoints;
preserve existing logs and immutable evidence. No scheduler state, provider
subscription, production database or active checkout is changed by this branch.

## October 1, 2026 follow-up checkpoint (Pacific time)

The primary Mac checkout is clean on `codex/reconciled-pipeline-updates` at
`ed484809ec48147bbe830f390609533f1c508f14`, which includes the merged MELI
three-row source publisher. The installed Windows application remains at
`7bcb09497d0aef92e358d0a7f3573f4dba262880`; merged source is not proof of
Windows deployment. The native filing-XBRL qualification change is preserved
separately at `58cb98427f5e9700a0886dd006015e99da9ce46b` and
`codex/parked/xbrl-native-qualification-20261001`. Its complete native kit is
still on HOLD after a Windows DLL initialization error (1114); no XBRL fact
write or processor approval follows from that candidate.

The canonical host's post-restoration check found the prior 44-task roster,
37 enabled tasks and no retained pause latch. FMP is externally disabled by the
owner's policy file; code merge does not reactivate it. The merged source publisher
covers exactly three reported MELI table rows, not the 28 operands required by
the separate MELI valuation recipe. No current MELI valuation or allocation
eligibility is accepted. A next session must compare the installed host source,
its typed source/admission receipts and the reviewed assumptions/scenario package
before claiming recovery or resuming a live refresh.

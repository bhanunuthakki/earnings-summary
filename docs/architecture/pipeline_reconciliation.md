# Pipeline reliability and inherited work

This is an implementation handoff, not a runtime health certification. Git `main`
is the executable source baseline. Only canonical directives own policy; pending
branches, draft research models, quality receipts and `.tmp/` artifacts do not
supersede it. Production database and background services belong to the configured
Windows authority, never a development checkout.

## October 2, 2026 current checkpoint

This checkpoint supersedes the release and branch states in the historical
checkpoints below. It does not certify current production health.

The working branch is `codex/reconciled-pipeline-updates`. Its executable
baseline is `origin/main` at `8149b5ab30650e122367941cd04da7d99e911c04`,
including Antigravity's merged PR1604. The three focused local code commits are:

| Local candidate | Purpose | Verified state |
| --- | --- | --- |
| `9250a2aa70ca5f4552035cd6a826f0932a2d79f1` | Bind processing, search and research to one immutable analysis evidence selection. | Full local gate: 17,310 passed, 66 skipped. Actual-head check passed. Native shared-file and lock checks remain required before production writes. Publication remains pending. |
| `a551cbc6099ad061627554b15029c811cf1a8b0f` | Check the Windows process termination signal before treating a job owner as active. | Full local gate: 17,330 passed, 68 skipped. Actual-head check passed. The two source-bound Windows function cases passed. Full Windows pytest and native failure-path testing were not run. |
| `9ca1e794d754f8c350ac86d79d3b5139280b1a1b` | Report a failed Windows Job handle close and still clean up the owned root process. | Full local gate: 17,337 passed, 68 skipped. Source-review pin and moved test-locator corrections preserve the strict checks. Native close failure-path testing was not run. |

These are local candidates, not accepted `main` or installed production code.
The separate publishing refs are `codex/analysis-evidence-scope`,
`codex/windows-job-owner-liveness` and `codex/windows-job-close-result`.
Do not force-push the old remote
`codex/reconciled-pipeline-updates` branch, which belongs to merged PR1604.
The complete pre-update branch remains at
`codex/parked/reconciled-before-main-sync-20261002`.
The working branch no longer tracks that retired remote branch. Publication uses
the three separate candidate refs above; no remote ref was changed.

The last verified installed Windows release is
`60b01e41c88f3202ed2793406493c5dfa2db07a4`. Resolve its current identity
and service state through the configured canonical host before live work.
The owner has confirmed that FMP remains inactive. Use SEC and issuer sources;
do not reactivate FMP or a paid fallback as part of recovery.

The filing reader remains unqualified. Six completed control measurements
show that the frozen C runtime and Python DLL initialize in both ordinary
and AppContainer processes. System32 `USER32.dll` initializes ordinarily but
fails in AppContainer with error1114. Every target passed read and execute
checks. Root replay verified all24 retained test files, exact restoration of
7,096 permission records, unchanged file records and restored token state.
Successful external process, TCP and UDP queries found zero task resources.
These are diagnostic results, not an accepted fix or financial extraction.
The exact initialization cause remains unknown.

The latest native job-owner test passed both exit-code cases, 0 and 259, while
process handles remained open. The unchanged test body used real Windows liveness,
creation-time and lock checks. Test launch controls added `CREATE_NO_WINDOW` and
an ownership handshake. This is two source-bound function cases, not a full
Windows pytest run or a native failure-path sweep. Root and an independent
advisory reviewer replayed all 51 retained files. All nine bound process handles
were released. A separate successful process, TCP and UDP check found all 19
retained task/launcher seeds and their descendants absent.

Earlier failures remain retained. A later startup observation identified an exact
Windows console process; it does not identify the original unrecorded descendant.
Current cleanup does not establish that missing historical identity.

The reader's revised command transport passed its Windows metadata preflight,
script parsing and eight controlled cleanup cases. Its test directory was absent
at preflight. The effective script policy was `RemoteSigned`; no policy changed.
These checks do not establish the reader's Windows interface result or qualify
financial extraction. Temporary diagnostic files and any later experiments retain
their separate ownership, source-hash, policy and cleanup checks.

The read-only Windows context inspection now succeeds. Its diagnostic retains
validated raw security information when an optional text conversion is unavailable.
The helper runs in a service session with enabled administrator membership.
This does not prove the restricted library failure's cause or qualify the reader.
No shared permissions, privileges or Windows interface objects changed. A separate
successful check found all retained task identities and descendants absent, with
zero TCP listeners and UDP endpoints. The census's own observer has reaped
transport evidence; it was not separately censused after exit.

The actual suspended-child token measurement also passed. The launcher has High
integrity; its AppContainer child has Low integrity and zero extra capabilities.
The child's package identity matches the independently derived existing profile.
The frozen C runtime probe loads successfully. Root and an independent advisory
reviewer replayed all 23 private receipts and exact restoration of 7,096 permission
records. Two later successful process, TCP and UDP checks covered all 91 retained
identity records and 104 process-number seeds, with no remaining task resources.
An earlier check counted its own connection after Windows reused a process number;
the later checks retained all original and new identities without exclusions or
termination. These results establish token facts, not a filing-reader fix. Private
Windows context creation, USER32 and SSL initialization, and financial extraction
remain unverified. Token restoration covers privilege lists and thread-token
presence; it does not establish equality of every token field.

The latest isolated startup test reached the expected Python helper. It stopped
before private context creation because its process group contained an extra,
unidentified process. A held process handle returned `WAIT_TIMEOUT` after cleanup;
the reason remains unknown. The test did not release the helper to create objects.
Its startup checks passed 42 local tests and six independent replay cases. A later
Windows check found all 20 retained identities and 44 process-number seeds absent,
with zero TCP listeners and UDP endpoints. Incomplete startup and historical
cleanup evidence remain explicit. This does not qualify the filing reader.

The local cleanup correction checks the native `CloseHandle` result. It retains
the handle and reports an error when the call fails. Startup and owner-loss paths
still try to stop the owned root process when handle close fails. This does not
establish the cause of the observed timeout. The correction adds no operator
command, scheduler action or stored-state change. Source-review records now name
the changed source and the two unchanged process-launch statements at their new
line numbers. The existing review classifications and strict test checks remain.
The separate process-only diagnostic is still local preparation. It sends no
creation release and has not run on Windows.

All seven installed-release valuation readiness results remain blocked.
MELI's verified SEC inventory is metadata completeness, not captured filing
bytes or admitted model inputs. Draft PR1603 retains an interim-statement
publisher; it is not a substitute for annual inputs, XBRL qualification,
semantic admission, model-role bindings or reviewed scenarios.

Pending XBRL work remains at
`58cb98427f5e9700a0886dd006015e99da9ce46b` on
`codex/xbrl-native-write-fence` and
`codex/parked/xbrl-native-qualification-20261001`. PR1590, PR1571 and
PR1445 remain separate pending work. Preserve their retained evidence and
state; none certifies the current candidates. No new valuation assumptions
or allocation recommendation has been accepted.

Next: complete native validation and filing-reader repair; publish and review
the exact candidates within owner approval; install through the canonical
release process; capture and admit the required issuer inputs; then refresh
valuations and run the two independent portfolio assessments. Personal
reserve and incoming-cash constraints remain in private task context. Do not
reuse old prices or holdings as current inputs.

Detailed private evidence remains under the ignored
`.tmp/pipeline-reliability-20261001/` and `.tmp/pipeline-scope-20261002/`
task directories. They are retained local evidence, not public source policy
or an operational database authority.

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

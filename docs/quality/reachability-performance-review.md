# Performance change reachability review

The isolated HEAD baseline `d442b5a4d141643f04f0bd4d7936348216febf53` has complete collection,
`PASS` production closure, and no disposition diagnostics. Source changes
invalidate its aggregate provenance hash. The production scanner remains
unchanged; no production dynamic edge was added.

All 173 existing decisions were checked against their baseline fingerprints
and current raw edge kinds/source text: 24 dynamic imports, 63 reflective
attribute accesses, and 86 process launches. No decision, target, or disposition
was added or removed. Internal targets remain subject to scanner validation.

One unchanged expression moved: `execution/comments_server.py:684` becomes
line 686, with `log = getattr(logger, level)`. Its existing `external_adapter`
disposition still selects standard logging methods. Only that entry's line
number/fingerprint changes; all other fingerprints remain unchanged.

The residual test-only inventory changes from 113 to exactly 122. Its nine
additions are bounded `node -` children with synthetic JavaScript supplied on
standard input and 10 second deadlines: two calls each in
`test_copilot_restore_read_lifecycle.py` and `test_panel_read_lifecycle.py`, two calls in `test_work_os_read_lifecycle.py`, and
one each in `test_policy_refresh_lifecycle.py`, `test_ui_requests.py`,
and `test_workspace_dcf_read_lifecycle.py`.
No baseline residual was removed. The oracle retains zero production unknown
and unresolved requirements and checks all nine test path/line pairs.

The three manifests bind to final source hash
`0eab6b244eeff5a3b00ad70e292fb9ea4f2e91d7161819429fe885c0c983db90` and unchanged scanner hash
`dffd867eda03733d7e0d9581dd6b8730d6c09f0a1b3d3245bba00b3c28e208db`. Both concurrent registered tests
(`test_backtest_prompt_candidate.py` and `test_eval_noise.py`) are included;
neither adds a dynamic edge.

Suppression ceilings match measurement across all buckets: 2,643 retained
Python files, 1,040 test suppressions, 1,070 execution suppressions, and 2,204
total. Nine obsolete canary directives were removed without new suppressions.

Validation: the final actual-tree test passes in 46.48 seconds. It confirms
complete collection, `PASS` closure, zero production unknown/unresolved edges,
and exactly 122 residuals. No scanner input changed after final review.
The full 68 reachability tests also passed before the final suppression and
inventory updates. Changed-suppression and diff checks pass. Pytest reports
10 cleanup warnings from older disposable test directories.

The isolated HEAD graph, final pre-update raw graph, and structured final
comparison are retained under `.tmp/quality/reachability-head-raw.json`,
`.tmp/quality/reachability-final-pre-review.json`, and
`.tmp/quality/reachability-performance-final-review.json`. They are review
proof, not operational state or deletion authority.

The final type diagnostic ceiling decreases from 1,928 to 1,924 for tests.
The configuration is the only changed scanner input. Restoring that one
number in memory reproduces the prior exact configuration hash. This data-only
change does not add or remove graph edges; aggregate source provenance includes
the new measured ceiling.

Picker correction follow-up: the frozen application correction adds one synthetic failure/partial/recovery test child. All 173 retained fingerprints and raw targets still match. The only scanner input changes since the final review are the runtime, its lifecycle test, the exact residual inventory, and the measured diagnostic ceiling. The current hash is `29c356d7d0685f81ec306e21d0c22c56a025e8bd154b4ad509e9f47858464525`.

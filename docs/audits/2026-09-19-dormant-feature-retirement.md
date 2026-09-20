# Dormant feature retirement — 2026-09-19

The cleanup retires three implementations with no current application, CLI, scheduler,
or registered dynamic caller. It does not activate deferred features or remove retained
user data. Inspection used base `f0d2130c62b1aee9e473466d9b93d2d624e1f8b1`; its tree matched
the preceding audit worktree at `b55ae89269ae94c40a06ae5aea90e151e5bf009c`.

| Retired implementation | Evidence and behavior removed | Retained behavior |
| --- | --- | --- |
| `src/research/drift.py` | Only `tests/test_drift.py` imported it. The historical Ledger plan described conviction-drift narration, but no runtime adapter converts captured `high`/`medium`/`low` conviction into its numerical series. Removes the manual deterministic summary and opt-in LLM phrasing API. | Decision capture/history, time-series primitives, and the distinct `thesis_drift_qoq` synthesis lens. |
| `src/research/dcf_tweak.py` | Only `tests/test_dcf_tweak.py` imported it. `research/run.py` produces a memo and optional saved-view proposal, never this adapter. Removes the manual natural-language question → bounded assumption edit → deterministic recomputation → inert DCF proposal API. | The in-app DCF editor, `/api/dcf/inputs`, `/api/dcf/recompute`, `/api/dcf/save`, DCF engine/drivers, proposal storage and approval/apply machinery. Manual editing is an alternative workflow, not an equivalent natural-language proposal feature. |
| `src/dcf/fact_sheet.py` | Only `tests/test_fact_sheet.py` imported it. Commit `4ba64b23` (#1506) already removed the fact-injection HTTP route and Explore controls; `test_explore_panel_removes_dcf_mutation_and_legacy_copilot_handoff` protects that boundary. Removes the manual companion-workbook read/upsert API. | Existing `dcf/facts/*.xlsx` files, canonical DCF workbooks, financial facts/provenance, and Explore analytics. No workbook is inspected, rewritten, or deleted by this retirement. |

The three dedicated test modules are removed with their implementations. Shared DCF,
research, driver, and approval tests remain unchanged. Two exact registry census
expectations change with their removed members: capture-quality specs 79 → 77 and
visual-emitter entries 156 → 155. All other registry behavior assertions are retained. The obsolete design
census entry for `research/dcf_tweak.py` is removed; no rendered surface changes.

The Ledger plan and capture program remain historical records. Their classification in
`directives/directive_manifest.json` is `history`; this retirement does not rewrite
previous owner intent into a claim that those features never existed. Repository-wide
symbol, import, CLI/config, scheduler, and documentation searches found no supported
manual entrypoint beyond the Python functions themselves. External ad-hoc Python
imports outside this repository cannot be ruled out by that inspection.

## LLM registration disposition

`drift_narrate` and `dcf_assumption_extract` are removed together from `LLM_MODELS`,
prompt versions, and capture-quality evaluation specifications. The unused
`DcfTweakPayload` / `DCF_TWEAK_SCHEMA` contract is removed. Their only application call
sites were in the retired modules. No model role, effective reasoning, provider,
transport, fallback, or budget policy is changed for a retained purpose. Historical
call ledgers, evaluation results, and budget/migration records are preserved.

The shared `/Applications/agent-instructions/config/llm_usage_index.json` was checked
read-only: earnings-summary remains registered through `src/llm/cli.py:LLM_MODELS`, with
the existing ledger, budget, evaluation, and separate Judge authorities. No shared
registration change is needed. No provider or model calls were made.

## Verification

Focused offline validation: **236 passed, one skipped**. This includes the original
127 adjacent retirement tests; setup, budget, routing, hard-stop and facade compatibility
regressions; prompt-registry byte-equivalence, batch-prompt and anchor tests. The skip is
the optional captured valuation prompt corpus, which is absent in the isolated worktree.
Tests ran serially with an external disposable pytest base directory; no provider calls
were made. All string literals in the pre-existing `llm_client.py` functions also compare
identically against the base source.

The complete 34-module `tests/test_llm*.py` family also passes: **473 tests**.
This exercises the retained LLM interfaces beyond the focused retirement cases.

All nine retained Python files pass whole-file Ruff lint/format and strict Pyright
(zero errors); the changed-file suppression scan returns no findings. The capture-quality
pruning test now exercises the public corpus loader with real synthetic purpose-sharded
and legacy JSONL files, asserting that unrelated shards are never opened and selected
shards are read. This preserves the file-I/O pruning contract independently of returned
value filtering.

Necessary compatibility typing removes the old CLI/facade suppressions without moving
state: public accessors read/write the existing `llm_client` setup globals, retaining the
monkeypatch surface and `_verify_setup_once()`'s `None` return. Public callable aliases
preserve the legacy private facade imports and subprocess patch surface. The renamed
`LLMBudgetExceededError` retains `LLMBudgetExceeded` as an alias to the same class.
The statistical anchor helper has a public name and retains its old private alias.
Prompt inputs gain precise mapping/quarter types; no prompt, provider, model, fallback,
budget decision, or setup behavior changes. An offline setup probe confirms the resolver
runs once, writes the original globals, returns `None`, and preserves exception identity.

Evidence: `.tmp/audit-tests/dormant-retirement-compatibility-tests.txt` and
`.tmp/audit-tests/dormant-retirement-gate-types.json`.
Root verification against the initial `f0d2130c` base found 3,036 whole-tree
Pyright diagnostics versus 3,095 before this change: 59 removed and no new
diagnostic identities in any file. Reconstruction and architecture checks pass.
Final counts must be measured again after the queued typing changes are integrated.
Shared reachability receipts, reconstruction inventory, ceilings, and full release
gates are owned by the integrating task and are not restamped by this change.

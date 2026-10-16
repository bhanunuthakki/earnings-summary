# Connected fact and calculation ownership

## Outcome

A financial value, its source badge, and the evidence opened from that badge must identify the same selected observation at the same knowledge cutoff. A valuation preview must use the backend's discount-rate formula when the user edits a driver. This change improves two current product paths. It does not retire historical records or change migration history.

## Plan

1. Preserve a typed canonical evidence reference in the existing `CellSource` model. Carry ticker, concept, canonical cell, selected observation, resolution revision, metric-definition revision, and timezone-aware knowledge cutoff from the admitted financial-table cell. Keep legacy source references supported.
2. Let the shared source-chip renderer link canonical references to a new read-only peek. Prefer the canonical reference over a mutable legacy document/fact reference. Financials level cells show their aligned canonical source chip beside each available canonical value, so an older period can open its own evidence instead of using only the latest row-label source. Missing or unsupported values show no evidence badge. Legacy row-source popovers keep their existing behavior. The badge retains the established design language. In scrolling Financials tables, it opens the existing source-viewer shell directly to prevent clipped native popovers. Other source chips retain their popover; its peek requests the explicitly validated fragment mode.
3. Reuse the complete `read_financial_table` projection at the referenced cutoff, then find the exact admitted cell. This preserves mixed-currency, ambiguous-coordinate and incomparable-history rejections that `_admit_cell` alone does not check. The projection performs its checks within one caller-owned SQLite read snapshot; the peek does not perform additional reads outside that snapshot. Check ticker, concept, cell, observation, resolution and metric-definition identities against that projection. File-based reports resolve evidence links only through their explicitly configured server origin; an absent or invalid origin disables those links. Later valid restatements or definition changes do not invalidate an old reference: re-admit at its original cutoff and return its original observation. Missing, mismatched or tampered evidence fails closed; never select the newest value or fall back to legacy authority.
4. Render verified value, unit, fiscal period, source locator, original wording when retained, and extraction/observation identity. Escape source content. State that this is the evidence for the selected value, with its cutoff. This first implementation shows retained admitted evidence metadata. It does not claim a raw-byte viewer when no viewer exists. Do not introduce file-path access or new persistence.
5. Put discount-rate derivation in one pure backend function in the existing DCF owner. Workbook reading and explicit driver-edit recomputation use it. Remove frontend CAPM arithmetic. Initial loading honors workbook WACC. CAPM driver edits and tax-rate edits select backend derivation; a direct WACC edit selects the existing preview-only override, until another driver or tax edit selects derivation again. Accept only explicit known derivation modes; omission preserves existing API callers' direct-WACC behavior. Return authoritative WACC and update the field only for the latest edit generation. Invalidate prior responses immediately on edit, including during the debounce interval. Reset and model reload invalidate pending recomputes. Reopening a ready editor resumes a preview cancelled on collapse. Save validates backend driver-derived WACC rather than a potentially stale preview override. Bound read recomputation deadlines and suppress obsolete failures as well as results. A save captures its input generation; later edits remain on screen after its response and are explicitly unsaved. Do not cancel/retry a consequential save or represent an old save as accepting newer edits. Keep direct-WACC edits visibly preview-only because the workbook save derives WACC from drivers.
6. Leave numerical builder/reader projection differences, tracker cutover, broad time-series migration and design-master replacement for separately bounded changes. They have wider semantic or live-state dependencies and are not needed to close these two paths.

## Ownership

| Decision | Owner | Consumer |
|---|---|---|
| Financial-table semantic admission and selected cell | `sources/report_financials.py` using canonical resolver, fact reader and ontology | Report and exact evidence peek |
| Immutable fact/evidence admission | `provenance/fact_read_model.py` | Existing financial-table reader |
| UI transport of selected evidence identity | Typed reference carried by `report.models.CellSource` | Shared source chip and read-only route |
| Source-chip links and anatomy | `ui/source_chip.py` | Report and supported dashboard chips |
| Request database lifetime and access | Existing content-route context and request-scoped read connection | Read-only canonical peek |
| Discount-rate formula | Existing backend DCF owner | Workbook reader and preview endpoint |
| Pending edits and latest response | Existing DCF editor | Backend request and returned valuation |

No owner can bypass another owner's validation. The frontend transmits selection identity and edit intent; it does not invent financial authority. All changes preserve provider policy, stable stored identities, source bytes, publication clocks, and recovery paths.

## Acceptance evidence

- Before implementation: independent review of this plan against the user outcome, current contracts, ownership, retention, request lifecycle and test sufficiency. Resolve blocking findings before dependent edits.
- A regression demonstrates that a canonical Financials value with no legacy document ID currently lacks an exact evidence link. After the change, the cell and clicked evidence share observation, resolution, definition, unit, period and cutoff identities.
- Verify malformed references, identity substitution, future/unavailable cutoffs, changed resolution/definition, source tampering, rejected cells, missing tables, and no fallback. Retain legacy chip/viewer behavior and request-owned connection lifetime.
- The evidence reference is a frozen, extra-forbidden typed model. Require bounded nonempty IDs and ticker/concept, a timezone-aware cutoff, and strict field types. Bound serialized reference length, reject duplicate or unknown query fields (only an optional single `fragment=1` is allowed), and validate before obtaining a database connection. Reject future cutoffs and unavailable earlier cutoffs without inventing proof. A different valid cutoff is a new request; this read-only reference is not a signed report-authentication token. All supplied identities must still match at that cutoff.
- Verify country-risk-inclusive driver recomputation, direct WACC overrides, later driver edits, tax-rate interaction, invalid payload handling, save/read compatibility, and late preview response suppression.
- Use synthetic migrated databases and existing hermetic fixtures only. Run targeted pytest serially. Rendering uses synthetic artifacts or supervised loopback test services with guaranteed cleanup; no production or provider effects.
- Capture before/after rendered evidence for both affected paths at 1440 and 1024 pixels, with relevant unavailable/error and interaction states. Check source text escaping, keyboard access and console errors. No new styling system.
- Run design sync, `tests/test_ui_controls.py`, and workspace golden comparison. Review any intended script golden update, then rerun comparison mode. Preserve protected behavior rather than weakening expectations. Record and test the operations-surface disposition: read-only evidence access adds no writer, operator action, scheduler or publication authority.
- Run format, lint, strict changed-file typing and applicable architecture checks. Use the normal push hook. The repository permits `FAST_PUSH=1` to preserve local security/privacy checks and delegate the full test/type matrix to CI; wait for the complete required matrix before merging.
- Obtain a separately briefed independent implementation review against the final diff and evidence. Resolve findings, bind its result to the reviewed commit, then push/create PR, wait for checks and merge. Re-review material changes or rebases that alter behavior.

## Authority and delivery

The owner authorized implementation, independent plan/result reviews, full push and merge. That authorization does not include live database changes or deployment. Existing unrelated work remains in the original checkout. This isolated branch begins at `origin/main`.

Reviews are ad hoc independent evidence under the judging procedure. They do not claim a calibrated statistical governance receipt. Missing proof is recorded as missing; a review cannot override failed tests or authorize an otherwise unapproved effect.

Status: implemented. Independent plan and implementation reviews passed after the documented repairs. Full CI and merge are required delivery gates; the pull request records their final state.

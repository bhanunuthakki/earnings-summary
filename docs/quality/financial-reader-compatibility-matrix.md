# Financial reader compatibility matrix

Source: `b5cca798463148eef44375a72a6ebde9058996c4`. Bounded read-only assessment of
legacy financial namespace selectors and document joins. Application source was
unchanged during this assessment. Existing test bodies were inspected, not run.
No database, fixture, source collection, or application change was created.

**Recommendation:** retain the namespace selectors and DCF authority join. Neither
has a proven equivalent replacement. Report and series duplicate a metadata query,
but that query already reads the native evidence ledger; it is not a competing
legacy fact reader. Its consolidation is a small code reuse candidate, not a
qualified retirement or a material data-model reduction. Proceed with the separate
selected-bundle reuse proposal when its independent gate passes.

`PASS` means the named field relationship is supported by inspected source/test
assertions. It is not a new test result. `FAIL` means a required replacement or
parity proof is missing. `RETAIN` means the distinction is required. No row below
qualifies a whole procedure for retirement.

## 1. Legacy financial namespace selectors

| Current owner and consumers | Fields and behavior | Existing replacement and disposition |
|---|---|---|
| `provenance/population_source_facts.py::_source_fact_from_row` (1343), `_planned_cell_id` (1790) | Publishes legacy `financial_facts` as `urn:earnings-summary:legacy:financial`; concept name, namespace, taxonomy, accounting basis, scope, unit, period and entity enter the retained cell identity. Financial basis/scope can remain `other`. | **RETAIN** immutable source identity. `MetricOntology` can bind a source cell to a canonical metric without changing it. Rewriting the namespace changes identity; it is not selector cleanup. |
| `sources/discovery_financials.py::read_financial_history` (170, query 193) | Candidates use legacy concept names OR `scope_constraints.financial_statement_concept`; joins bound canonical cells and definitions at the cutoff. Candidate concept/period order and unresolved entries survive partial reads. Native statement roles are verified per observation (297); the legacy analytics path can preserve explicit `other` scope without decision-grade status. | **PASS** existing reviewed-role route for native observations. **FAIL** replacement of the legacy arm: no complete legacy-to-reviewed-role population parity. Using canonical names alone loses the role distinction or changes the candidate population. Retain both arms. |
| `sources/report_financials.py::read_financial_table` (356, query 385), `_admit_cell` (204) | Candidates accept legacy concept, canonical metric name, OR reviewed statement concept; derived candidates can trace issuer through inputs. Native roles and scope labels are verified (270). Unqualified scope becomes a reason, not silently consolidated. | **PASS** native reviewed route, including combined/carve-out scope. **FAIL** deleting legacy selection: equivalent names, duplicates, cutoff population and failure reasons are not established for all retained legacy cells. Retain the selector and local report policy. |
| `sources/canonical_financial_series.py::discover_canonical_financial_tickers` (247), `_project_coordinates_uncached` (439), `_read_uncached` (728) | Candidates use legacy concept OR a bound canonical metric name. Selection checks repeat that alternative (651, 987). Period/cadence, admission, lineage and continuity determine the series; the reader does not implement discovery's partial-reference or report-role projection. | **PASS** native binding route. **FAIL** replacement by discovery/report selection: their statement-role key, partial results and failure scope differ. **RETAIN** series semantics; do not alias these readers. |
| `sources/canonical_financial_series.py::financial_metric_catalog` (1230) | Lists legacy namespace concepts and non-legacy bound canonical names. Counts/names expose available vocabulary, not admission. `include_legacy=True` additionally lists raw `financial_facts`, explicitly used by Ask grounding. | **RETAIN** catalog/admission distinction and explicit compatibility option. **FAIL** bound-only replacement: it would hide retained but unbound names. No usage evidence establishes that this behavior is unused. |
| `sources/dcf_statements.py::read_dcf_statements` (350, query 361) | Candidates require the legacy financial namespace, approved `_MAPPING` concept, reported quarterly coordinate, and bound canonical cell. Source eligibility is checked later. Reads use the existing batch resolver. | **FAIL** native replacement: the existing reader has no reviewed-statement-role candidate route equivalent to its legacy concept map. Removing the predicate would broaden input semantics; keeping only native names would remove current actuals. Retain pending exact candidate/population proof. |

Evidence already in the repository:

- `tests/test_financial_statement_admission.py::test_native_qname_needs_exact_review_and_keeps_combined_scope`
  asserts report and discovery exclude the native fact before admission, then retain
  the reviewed observation and `combined_carve_out` label. It also preserves the
  original bundle and historical cutoff. This proves a native route, not legacy parity.
- `tests/test_financial_source_repairs.py::test_financial_reader_uses_native_reviewed_binding`
  proves a native QName can supply a series through binding; the adjacent
  `test_unbound_native_name_cannot_supply_financial_series` rejects a matching name
  without admission.
- `tests/test_dcf_canonical_statements.py::test_canonical_round_trip_preserves_reported_zero_and_exact_bridge_lineage`
  protects current actuals, zero capex and serialized lineage. The cutoff,
  definition-change and ambiguous-cell tests protect additional DCF distinctions.
- `tests/test_financial_consumer_series.py::test_canonical_names_reach_condition_vocabulary`
  exercises catalog use. Direct catalog consumers are `decision_conditions.py:655`,
  `ask/grounding.py:955` and `viewspec/engine.py:1163`.

Missing capability for selector retirement: an existing authoritative mapping that
covers the same retained legacy and native candidates, including rejected/unbound
names, concepts, periods, scope labels and cutoff history. Neither a canonical name
nor a reviewed-role definition alone currently supplies that union. Required proof
is same-population output/order/gap parity for each consumer, not equal values on
one admitted fact. No new selection framework or population migration is proposed.

## 2. Legacy document joins and evidence metadata

| Current owner / field | Existing evidence owner and parity | Retirement decision |
|---|---|---|
| DCF `_read_cell`, `sources/dcf_statements.py:323`: `legacy.id`, legacy ticker, evidence-version ticker, `source_type`, `source_quality_tier`, observation URL | `evidence_ledger.DocumentVersion` (333) owns version identity, issuer/ticker and optional legacy ID; `SourceObservation` (309) owns source kind/URL/retrieval. Neither contains legacy source type or quality tier. `ExactEvidenceReference` (`fact_read_model.py:78`) supplies exact lineage, not those fields. | **FAIL** replacement of `JOIN documents`. The missing capability is an equivalent authority projection plus both ticker checks. A source URL, form or source kind is not that authority. |
| `dcf/primary_fact_overlay.py::primary_source_eligible` (262) | Accepts type `sec_xbrl`/`ir_doc` OR tier `sec_official`. This is the existing shared DCF policy; it does not infer eligibility from native evidence presence. | **RETAIN** the policy and its call. `test_actual_resolver_rejects_inadmissible_coordinates[non_primary]` changes legacy type/tier to FMP/aggregator and requires unavailable actuals. Bypassing the join would defeat that assertion. |
| Report `_admit_cell`, `sources/report_financials.py:330`: legacy ID, source URL, kind, retrieved time | Already joins only `evidence_document_versions` and `evidence_source_observations`, keyed by the selected bundle's exact document version. It does **not** join `documents`. Missing evidence/metadata yields absent display fields. | **PASS** native-ledger field authority; **RETAIN** optional legacy ID for compatibility. No legacy document dependency to retire here. |
| `sources/canonical_financial_series.py::read_financial_consumer_series`, query 1378: same four metadata fields | Reads the same native ledger records and exact version. Reorders the columns and converts ID/time/text for `FinancialConsumerPoint`. Report projects its own `FinancialTableCell`. | **PASS** underlying field correspondence; **FAIL** a ready shared-reader replacement: inspected ledger API exposes persistence, not this metadata read projection. A thin shared read could replace the two queries, but coercion/null/error/snapshot parity needs focused proof. Keep consumer DTOs; do not construct a new fact model. |

`tests/test_report_canonical_financials.py::test_report_canonical_cell_links_exact_evidence_without_legacy_document`
asserts rendering, source navigation and exact evidence reopening with `doc_id=None`.
`tests/test_canonical_financial_peek.py::test_source_link_opens_exact_value_without_legacy_rows`
protects the related reader path. Series metadata serves Ask, conditions and Explore
(`ask/grounding.py:786`, `decision_conditions.py:898`, `viewspec/engine.py:386`).
`test_ask_cites_each_canonical_period_document` and the snapshot/connection tests in
`tests/test_financial_consumer_series.py` protect those retained source references.
These assertions do not prove DCF eligibility without its legacy join.

## Decision and stopping point

The two matrix rows are complete for this bounded scope. There is no all-PASS
procedure retirement. The missing authority is precise: complete selector mapping
for row 1, and source-type/tier authority plus ticker parity for the DCF part of
row 2. Report/series metadata query reuse is possible code consolidation, but does
not remove durable state or financial admission complexity. Do not rank it above
the already-scoped bundle reuse merely to report a removal.

A later implementation can qualify one cluster only after its exact replacements
and same-input behavior pass. Before legacy module removal, apply the transitive
reachability requirement in `directives/agent_implementation_traps.md`. Physical
retirement remains separately gated by `directives/data_provenance.md` section 11.
No further audit, schema work, new guard, or feature deletion is implied here.

## Existing-owner clarification

Astra's subsequent architecture review corrects a narrow interpretation above:
an existing owner need not already expose the replacement helper. A module-level
`read_document_source_metadata(conn, document_version_id)` in `fact_read_model`
can own the one four-field native-ledger query. Both consumers already import that
owner. Keep conversions, optional evidence, exceptions and snapshots local; add no
cache, admission rule, fact model, or DCF authority fields. This is a valid modest
procedure-consolidation candidate once paired metadata/null/error parity and
unchanged query counts pass. It is not yet implemented or qualified, and it does
not remove a durable representation. The missing DCF authority remains unchanged.

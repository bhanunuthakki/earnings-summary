# Directives

`directive_manifest.json` is the complete machine-readable index. Every Markdown
file under this directory has exactly one of four classes:

- **canonical** — current decision or product contract; may govern implementation;
- **runbook** — current procedure that governs only when its task is invoked;
- **draft** — exploration or proposal; non-governing until explicitly promoted; or
- **history** — retained evidence that cannot override current authority.

Inline labels in older documents are historical prose. When they disagree with the
manifest, the manifest wins. Changing a class requires editing the manifest and
passing `make instruction-check`; renaming or adding a directive without classifying
it fails the same gate.

## Current authority map

| Concern | Canonical owner | Adjacent procedure or evidence |
|---|---|---|
| pipeline stages and repeat safety | `data_pipeline_dag.md` | source identities in `data_provenance.md` |
| provenance and source precedence | `data_provenance.md` | run-specific ingestion runbooks |
| repository topology | `folder_structure.md` | `execution/validate_folder_contract.py` |
| visual system and page continuity | `design_language.md` | `design_conformance_audit.md` runbook |
| interaction behavior | `interaction_contract.md` | `report_comments_and_chat.md` |
| operational surface changes | `operations_governance_surface.md` | named operation runbook |
| LLM call and fallback boundary | `llm_calls.md` | provider adapter runbooks |
| LLM quality and failure evidence | `llm_evals.md` | cases and rubrics under `evals/` |
| model qualification and promotion | `model_eval_loop.md` | economic rule in `cheapest_model_routing.md` |
| private hosting architecture | `self_host_scoping.md` | `self_host_phase1_laptop.md` runbook |

Product code, typed schemas, migrations, executable registries, and tests remain
more precise authorities when a directive explicitly delegates a value or inventory
to them. Current roadmap and open-work status live in Linear team `BHA`; dated plans
and backlog files are history unless the manifest says otherwise.

## Task routing

- Start with the relevant canonical contract, then load only the named runbook.
- Drafts may inform exploration but cannot authorize implementation or contradict a
  canonical owner.
- History may explain why a decision exists but cannot reopen work or supply current
  model, price, status, or repository facts.
- If two canonical files appear to own the same decision, stop and consolidate the
  boundary instead of choosing whichever prose is newer.

Run the standalone checks with:

```text
PATH=.venv/bin:$PATH PY=.venv/bin/python make instruction-check
```

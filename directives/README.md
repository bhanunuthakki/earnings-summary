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

## Authority graph

The manifest is the complete authority graph; this README does not maintain a second,
partial map. Each canonical entry declares one or more stable `authority_domains`.
The validator rejects a domain claimed by more than one canonical file. Each runbook
entry declares the canonical contracts it implements in `governed_by`; the validator
rejects missing, non-canonical, or repeated targets. Draft and history entries cannot
carry either field.

The metadata makes placement reviewable and prevents exact duplicate ownership. It
does not pretend to understand prose: when a proposed domain is merely a synonym for
an existing one, consolidate it into the existing canonical owner rather than minting
a second label. Add a new domain only for a genuinely independent product decision.

Product code, typed schemas, migrations, executable registries, and tests remain
more precise authorities when a directive explicitly delegates a value or inventory
to them. Current roadmap and open-work status live in Linear team `BHA`; dated plans
and backlog files are history unless the manifest says otherwise.

## Task routing

- Find the relevant `authority_domains` entry, then load only the runbooks whose
  `governed_by` list names that canonical contract.
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

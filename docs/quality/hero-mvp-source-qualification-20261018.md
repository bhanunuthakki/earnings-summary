# NU and MELI source qualification — October 18, 2026

This is an isolated source qualification record. It does not establish accepted
briefs, financial admission, production deployment or a complete research snapshot.

## Owner scope and measured acquisition

The owner selected NU Q2 2026 as a labelled historical pre/post reconstruction.
The owner selected MELI’s latest published period at qualification. The MELI
brief, business assessment, DCF assumptions, scenarios and workbook remain one
integrated acceptance case.

The bounded pass selected 12 documents: six newly captured SEC primary filings,
five hash-verified retained issuer/SEC documents, and one retained SEC
CompanyFacts snapshot. No additional documents were collected after reaching
the limit. Selected raw sources total 19,188,589 bytes. Retained inventory and
package metadata total 291,985 bytes. New HTTP response bodies total
12,385,096 bytes across 22 requests. The pass stays below 250 MiB.

Fresh complete SEC submissions metadata identifies June 30, 2026 as MELI’s
latest declared periodic reporting period. This establishes the reporting anchor;
it does not establish complete filing processing or extraction.

## Direct source checks

- [MELI Q2 2026 10-Q](https://www.sec.gov/Archives/edgar/data/1099590/000109959026000023/meli-20260630.htm):
  all 18 interim statement roles and all three reported-table roles pass the
  existing candidate parsers. Native hierarchy and text spans replay against the
  complete captured HTML. No synthetic document wrapper was used for this check.
- [MELI FY2025 10-K](https://www.sec.gov/Archives/edgar/data/1099590/000109959026000006/meli-20251231.htm):
  all seven annual flow roles pass the new closed annual recipe against the
  complete captured HTML. Native hierarchy and source spans replay. A second
  real-source pass with the changed code also preserves all 18 interim and three
  reported-table candidates. These are 28 candidate model roles, not admitted
  model inputs.
- [NU Q2 2026 financials](https://www.sec.gov/Archives/edgar/data/1691493/000129281426004225/nufs2q26_6k.htm),
  [original Q2 2025 financials](https://www.sec.gov/Archives/edgar/data/1691493/000129281425003063/nufs2q25_6k.htm),
  Q2 releases, retained original Q1 2026 financials and retained Q1/Q2 call PDFs
  are captured or hash-verified candidates. Exact quarterly subject witnesses and
  the historical public evidence selection remain separate requirements.
- Retained MELI CompanyFacts bytes pass the native source proof, including exact
  issuer/CIK, immutable hash, source URI and capture-clock checks. Its standard
  taxonomies cannot supply every required MELI business split. CompanyFacts can
  run independently of the external filing-XBRL processor.

The selected MELI Q2 package lists 93 files, including 82 financial attachments.
The annual package lists 145 files, including 131 financial attachments. Those
attachments were inventoried, not captured under this 12-document pass.

## Boundaries and remaining acceptance

The configured filing-XBRL processor resolver returns
`installation_not_configured`. This blocks that native processing lane; it does
not block the independent CompanyFacts lane. No installation or sandbox change
was attempted. Public filing dates are not exact publication timestamps. SEC
acceptance is an available-by witness for that archived SEC copy, not proof of
the issuer’s earliest release time.

Historical source selection is implemented separately from historical financial
snapshot acceptance. Cross-review confirmed that an issuer-wide fact projection
could otherwise include post-event facts despite the historical text selection.
The v2 financial snapshot path therefore fails closed with
`historical_research_fact_source_closure_unavailable` until reported and derived
fact lineage is constrained to the selected original source versions.

Remaining gates include complete required package processing, source population
publication and semantic admission, one sealed research selection, claim-level
narrative alignment, integrated economic assumptions/scenarios and a recalculated
workbook. Candidate counts must not be described as admitted model inputs.
Production database writes, canonical admission and deployment were not performed.

## Code verification

The changed-file gate passes 60 tests, formatting, Ruff and strict Pyright with
zero errors. The original historical scope compatibility checks pass 98 cases.
Independent cross-review found authority-order and physical identity-clock gaps;
regressions fail before the fixes and pass afterward. A separate regression holds
historical financial snapshots in both verifier modes.

The architecture measurement admits 393 additional production noncomment lines
in existing owners, with zero new executable modules. The old LOC ceiling failed
before its explicit refresh. No other growth metric, scanner, fan-out, cycle or
duplication limit was increased. Existing literal attribute dispositions move to
their new source lines; their classifications and target sets remain unchanged.

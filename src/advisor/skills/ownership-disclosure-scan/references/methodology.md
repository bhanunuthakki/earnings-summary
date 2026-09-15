# Ownership-disclosure methodology

## Form 13F

Primary authority: SEC EDGAR submissions, filing documents, and official Form 13F
data. Resolve managers to CIKs and select filings by `reportCalendarOrQuarter`,
not merely by the latest filing dates. Search current submissions and referenced
submission-history files.

For each manager and period, enumerate `13F-HR`, `13F-HR/A`, `13F-NT`, and
relevant amendments. Read the cover page: a restatement replaces the effective
information table; an amendment that adds holdings supplements the effective
table. Apply multiple amendments in filing order while retaining every accession.
Confidential-treatment, notice, combination-report, or unclear-amendment coverage
makes the package partial unless the effective holdings can be reconstructed.

Reconcile information-table row count and total value to the filing Summary Page.
Diff current and prior effective packages using CUSIP, class, put/call,
share/principal type, and relevant discretion/other-manager fields. Use share or
principal quantity—not market value—to classify `new`, `add`, `trim`, `exit`, or
`unchanged`. Mark corporate actions and unresolved security-identity changes
`ambiguous`. Keep options distinct. Do not infer shorts, net exposure, or an
executed trade from long holdings snapshots.

Report each package's report period, filed date, accession, form, official URL,
amendment disposition, reconciliation result, and ticker-mapping coverage by row
count and reported value. Call out the usual reporting lag and securities omitted
from 13F coverage.

Official references:

- SEC Form 13F FAQ: https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f
- SEC Form 13F data sets: https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
- SEC filing technical specifications: https://www.sec.gov/submit-filings/technical-specifications

## U.S. congressional Periodic Transaction Reports

Primary authorities are the House Clerk financial-disclosure portal and Senate
eFD public search. Record chamber, official report ID/version, filer, owner
(self/spouse/dependent when reported), filed date, transaction date, transaction
type, asset description, disclosed ticker or explicit mapping status, amount
range, page/row locator, and official URL.

Keep purchases, sales, and exchanges distinct. Preserve amount bands; never turn
them into exact dollars or midpoint estimates without labeling a separate
calculation. An ownership label does not prove who directed the transaction, and
a filing does not prove intent or an informational advantage.

Search and reconcile originals, corrections, and amendments. A correction may
supersede the effective view, but retain both versions; unclear replacement
semantics remain unresolved. Report House and Senate acquisition completeness
separately, including query scope, result/page counts, fetched documents, parsed
documents, and failures. Portal agreement, CAPTCHA, availability, or parse failure
means `partial` or `unknown`, never zero activity.

Periodic reports can arrive after the transaction. A filing-window scan must show
transaction dates separately; a transaction-window scan needs enough forward
filing buffer to cover the statutory reporting window. Senate public online scope
does not establish staff-level completeness.

Official references:

- House financial disclosures: https://disclosures-clerk.house.gov/FinancialDisclosure
- House Periodic Transaction Report calculator: https://ethics.house.gov/periodic-transaction-report-calculator/
- Senate financial disclosures: https://www.ethics.senate.gov/public/index.cfm/financialdisclosure
- Senate eFD search: https://efdsearch.senate.gov/search/home/

## Coverage receipt

For every target or chamber, emit:

```text
target · requested period/window · official queries/pages · results enumerated
documents fetched/parsed · amendment disposition · reconciliation/mapping status
coverage = complete|partial|unknown|failed · gaps
```

Aggregate coverage is never stronger than its weakest in-scope component. A
complete zero-result receipt is a finding; an incomplete search is a gap.

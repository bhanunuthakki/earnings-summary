# FPI/MJDS Data-Availability Spike — Findings

**Date:** 2026-07-18 · **Scope:** the Phase-3 hard prerequisite from
`docs/design/segment_quarterly_framework.md` §1.1/§7.1 — does FMP's
`financial-reports-json` (as-filed SEC report JSON) return any usable interim
(Q1–Q3) data for FPI filers (20-F: NU/ASML/NVO; 40-F/MJDS: BN/BAM)?
**Probe artifacts:** `.tmp/fpi_spike/` (scripts + raw result JSONs, keys redacted).
**FMP calls spent:** 22 of the 40 budgeted.

## Verdict on the design-doc fork

**NOT-AVAILABLE. The 6-K / IR-deck route is the only path for FPI quarterly
segment data.** Direct `financial-reports-json` probes all returned tier-gated
402s (see caveat), but the verdict does **not** rest on them: it rests on
`financial-reports-dates`, which serves gated symbols fine (HTTP 200) and is the
roster of exactly what `financial-reports-json` can serve (one `linkJson` per
entry). That roster, fetched live today, contains **zero Q1–Q3 entries for any
FPI probed** while the domestic control shows a full interim series:

- **MELI** (10-K control): 58 entries — Q1/Q2/Q3/Q4/FY series through 2026 Q1.
- **NU** (20-F): 2 entries — 2021 Q4 + 2021 FY only. Nothing since (roster
  stale even on the annual axis; NU's 2022+ 20-Fs are absent).
- **ASML** (20-F): 6 entries — FY/Q4 only (2016/2019/2022/2024/2025). No Q1–Q3.
- **NVO** (20-F): 9 entries — strictly FY+Q4 pairs 2021–2025 ("Q4" almost
  certainly aliases the annual 20-F dataset). No Q1–Q3.
- **BN** (40-F): 3 entries — 2024 Q4 + 2024 FY + one stray 2023 Q2. No series.

This matches the structural expectation: FMP's as-filed report JSON mirrors
SEC-rendered Financial Report ("R-file") data, which exists for 10-K/10-Q
filings; FPI 6-Ks are furnished exhibits outside that pipeline. There is nothing
behind the endpoint to unlock at any tier.

## Tier caveat (explicit)

The account currently behaves as **free tier**: AAPL (free-tier symbol) returned
200 on both `income-statement?period=quarter` and `financial-reports-json`
(2025 Q1 — a real 45-key 10-Q payload **with four "Segment Information and
Geography" note tables**, incidentally validating Phase 1's premise), while
MELI/NU/ASML/NVO/BN all 402'd with "Premium Query Parameter: 'symbol'" on every
statement/report endpoint — including MELI, the domestic control, and endpoints
that succeeded in May under the prior plan. Cached `fmp_endpoint_status` shows
429 "Limit Reach" waves (07-17), consistent with a 250/day cap. **Every 402
observed here is therefore inconclusive for Starter.** The dates-roster evidence
above is tier-independent, so the verdict stands; still, after the Starter
upgrade, spend 2–3 confirmation calls (BN 2023 Q2, NVO 2024 Q4, ASML 2024 Q4) to
characterize the stray non-FY entries (expected: annual-report aliases or
one-off XBRL-furnished 6-K statements, not a usable series).

Contrast check (probe 5): standardized quarterly statements for FPIs exist —
`ASML_income_statement_quarterly.json` (100 rows to 2026-03-31) and NU's
equivalent are on disk from the prior plan. Standardized quarterly ≠ as-filed
note tables; only the latter is missing.

## Recommended Phase-3 routing per filer

| Filer | Regime | Route | Confidence |
|---|---|---|---|
| NU | 20-F | Keep bespoke IR-spreadsheet pipeline (untouched) + 6-K/IR LLM extraction for segments | High |
| ASML | 20-F | IR quarterly report (6-K-furnished press release; US-GAAP tables) via IR-doc LLM route | High |
| NVO | 20-F | IR quarterly interim report PDF via IR-doc LLM route | High |
| BN | 40-F | Quarterly Supplemental Information PDF (as designed, §1.1) | High |
| BAM | 40-F | Same MJDS Supplemental/IR-PDF route as BN | Medium-high (inferred, not probed) |
| BHP | 20-F | Half-yearly IR reports only — BHP does not report quarterly segments; expect H1/H2 granularity, use the existing `H1`/`H2` enum | Medium (domain knowledge, not probed) |

Also observed: no `*_form_10q_*.json` exists on disk for **any** ticker (count
0) and `fmp_endpoint_status` has no `financial-reports-json` quarterly rows —
`fetch_fmp_10q_json.py` has never landed a payload under the current key, so
Phase 1's 10-Q fetch is itself gated until the Starter upgrade completes.

## Appendix — raw probe statuses (2026-07-18, all `stable/` base)

| # | Probe | Params | HTTP | Result |
|---|---|---|---|---|
| 1 | financial-reports-dates | NU | 200 | list[2]: 2021 Q4, 2021 FY |
| 2 | financial-reports-dates | BN | 200 | list[3]: 2024 Q4/FY, 2023 Q2 |
| 3 | financial-reports-dates | MELI | 200 | list[58]: full Q1–Q4/FY series |
| 4 | financial-reports-dates | ASML | 200 | list[6]: FY/Q4 only |
| 5 | financial-reports-dates | NVO | 200 | list[9]: FY+Q4 pairs only |
| 6 | financial-reports-json | NU 2025 Q1 | 402 | symbol-gated ("Premium Query Parameter") |
| 7 | financial-reports-json | NU 2024 Q2 | 402 | symbol-gated |
| 8 | financial-reports-json | ASML 2025 Q1 | 402 | symbol-gated |
| 9 | financial-reports-json | ASML 2024 Q2 | 402 | symbol-gated |
| 10 | financial-reports-json | ASML 2024 Q4 | 402 | symbol-gated |
| 11 | financial-reports-json | NVO 2025 Q1 | 402 | symbol-gated |
| 12 | financial-reports-json | BN 2023 Q2 | 402 | symbol-gated |
| 13 | financial-reports-json | BN 2024 Q4 | 402 | symbol-gated |
| 14 | financial-reports-json | BN 2025 Q1 | 402 | symbol-gated |
| 15 | financial-reports-json | MELI 2025 Q1 (control) | 402 | symbol-gated — control blocked ⇒ tier confound proven |
| 16 | financial-reports-json | MELI 2025 Q3 (control) | 402 | symbol-gated |
| 17 | income-statement | NU quarter | 402 | symbol-gated (worked under prior plan; cache on disk) |
| 18 | income-statement | ASML quarter | 402 | symbol-gated (100 quarterly rows cached from May) |
| 19 | income-statement | AAPL quarter | 200 | list[4] — free-tier symbol works |
| 20 | financial-reports-json | AAPL 2025 Q1 | 200 | dict[45 keys], 4 segment-note tables — endpoint itself serves interims for 10-Q filers |
| 21 | income-statement | MELI quarter | 402 | symbol-gated |
| 22 | profile | MELI | 200 | list[1] — profile ungated even for premium symbols |

No rate-limit headers were returned on any response. No writes to portfolio.db
or the cost ledger; all artifacts under `.tmp/fpi_spike/`.

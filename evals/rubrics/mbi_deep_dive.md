# Rubric: mbi_deep_dive (v2)

Pass threshold: 0.85
Scope: Long-form equity research briefs, MBI-style deep dives, and full thesis underwriting memos.
Grading convention: 1.0 = clear pass, 0.5 = partial / surface-level, 0.0 = fail.

Include: _shared_mbi_definitions.md

## Facet: materiality_and_thesis_selection [weight: 0.20] — Identifies what actually decides this business
- **1.0:** Exercises independent investing judgment to center the memo on the 2–3 load-bearing operational and economic debates that govern the company's 5–10 year compounding outcome. Allocates depth proportionally to what moves the needle rather than filling a generic finance checklist.
- **0.5:** Covers accurate facts across many topics equally without isolating which debates are load-bearing for long-term equity value.
- **0.0:** Focuses primarily on short-term quarterly noise or generic boilerplate rather than the business's decisive long-term questions.

## Facet: operational_and_call_reality [weight: 0.30] — Explains how the business operates on the ground
- **1.0:** Explains how the company actually acquires and retains customers, builds primacy or switching costs, underwrites/prices/delivers product, monetizes cohorts over time, manages funding or unit costs, and competes across its industry value chain — grounded concretely in product mechanics, filing disclosures, and earnings-call Q&A rather than abstract financial ratios. Shows the underlying data in compact multi-year operating tables (when available). When doing a DCF revision, major model revision, or underwriting a major structural unit-economics debate, adds an optional illustrative unit-economics / break-even sensitivity table quantifying exact arithmetic for the central debate.
- **0.5:** Stays mainly at the headline KPI or financial-statement level (`ARPAC × customers`, `ARR × NRR`, consolidated margins) without showing multi-year operating data or the operational mechanics underneath.
- **0.0:** Reads like a financial-spreadsheet summary detached from how the business actually operates.

## Facet: empirical_thesis_interrogation [weight: 0.20] — Tests specific empirical theses against evidence
- **1.0:** Tests each specific empirical claim against hard evidence and steelmanned counter-arguments: catches metric smoothing, scope mismatches, one-off tailwinds, Q&A evasions, or self-selection bias; tests the real operational breaking mechanism (such as credit-loss allowance absorption, capital strain, or FX/macro transmission for lenders; expansion-meter limits and cost deleverage for software) against historical reality.
- **0.5:** Summarizes management explanations or lists generic risks without testing whether the operating data actually establishes the claimed mechanism.
- **0.0:** Parrots management claims uncritically or presents a one-sided pitch without empirical testing.

## Facet: whats_in_the_price_candor_and_imagination [weight: 0.20] — What's in the price, low-confidence candor, and testing for lack of imagination
- **1.0:** Clearly inverts the **latest live market price** (using bounded sensitivity if the saved model snapshot predates a share-price move) into what business outcomes, reinvestment needs, and compounding durability are already priced in for an owner to clear a long-term IRR hurdle (separating business quality from entry price). Mandatory table stakes: includes a **summary DCF driver snapshot (Table B)** bridging multi-year historical actuals (`A`) into explicit forecast driver rows (`E`) with full-horizon visibility — including near-term years plus the Year-10 terminal column, WACC, exit basis/multiple or perpetuity `g`, and implied perpetual FCF growth for 10-year DCFs. Candidly names which multi-year forecast rows are low-confidence guesses rather than faking precision, AND explicitly tests whether the market price — or a mechanical fade model — suffers from a lack of imagination about multi-year compounding (such as cohort ARPAC compounding across many years and multiple markets) alongside honest downside risk.
- **0.5:** Quotes multiples, DCF targets, or sensitivities mechanically without a historical-to-forecast driver snapshot (Table B), relies only on a stale snapshot price without inverting the latest live price, or fails to test low-confidence assumptions and upside imagination vs. downside risk.
- **0.0:** Omits valuation/capital reality completely, hides behind CAPM/spreadsheet jargon instead of business outcomes, or equates a good company with a safe stock.

## Facet: independent_mbi_voice [weight: 0.10] — Plain-spoken solo-investor judgment and clarity
- **1.0:** Reads like an independent solo investor thinking clearly on paper: cohesive, engaging prose, zero sell-side hype, zero trade directives (`Buy`, `Sell`, `Hold`), no template filler, zero leaked internal pipeline/file/JSON identifiers (such as `"the packet"` or raw schema field names), and closes with the concrete operational evidence over time that would confirm or refute the specific empirical theses.
- **0.5:** Mostly clear, but slips into robotic template phrasing, mild cheerleading, leaked internal pipeline/schema jargon, or generic closing watch-items.
- **0.0:** Consultant-deck boilerplate, wall-of-bullets clutter, or sell-side marketing tone.

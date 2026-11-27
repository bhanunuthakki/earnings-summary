# Rubric: post_earnings_readout (v2)

Pass threshold: 0.80
Scope: Post-earnings analytical readouts synthesizing a company's release, presentation, and earnings-call Q&A against multi-year empirical theses and valuation reality.
Grading convention: 1.0 = clear pass, 0.5 = partial, 0.0 = fail.

Include: _shared_mbi_definitions.md

## Facet: operational_signal_vs_noise [weight: 0.25] — Separates durable operational signal from quarterly noise
- **1.0:** Looks past headline beats/misses to what actually changed in customer acquisition, cohort monetization, underwriting/unit delivery, funding or cost structure, and competitive position — anchored in a compact multi-quarter operating driver table distinguishing seasonality, FX translation, or one-off boosts from lasting operational inflection.
- **0.5:** Summarizes reported revenue, margins, and KPI deltas accurately without a multi-quarter trend view or digging into the underlying operational mechanisms.
- **0.0:** Pure press-release recap that takes headline non-GAAP results at face value.

## Facet: call_qa_and_say_do_interrogation [weight: 0.20] — Audits Q&A concessions, smoothing, and forward math
- **1.0:** Intersects the slides with the earnings-call Q&A: catches management concessions, metric scope mismatches, smoothing, disappearing disclosures, and implied H2/full-year guidance deceleration or reinvestment tradeoffs.
- **0.5:** Notes guidance changes cleanly, but misses Q&A tensions, metric smoothing, or disclosure omissions.
- **0.0:** Parrots prepared remarks uncritically.

## Facet: price_implied_path_candor_and_imagination [weight: 0.25] — Re-reads price-implied path with candor and compounding lens
- **1.0:** Updates a mandatory compact historical-to-forecast driver snapshot (Table B, with full-horizon/terminal visibility when referencing a 10-year DCF) showing what the quarter changes in the 5–10 year operating path, capital retention / dilution burden, and prospective IRR vs. hurdle against the **latest live market price** (using bounded sensitivity if a model snapshot predates the move) — separating "stock moved X%" from "multi-year compounding moved Y%." Adds an optional unit-economics sensitivity grid (Table A) when triggered by a major model/DCF revision. Candidly flags remaining low-confidence assumptions and tests whether the market is under-imagining long-run compounding or extrapolating temporary strength.
- **0.5:** Mentions valuation multiples loosely without the historical-to-forecast driver snapshot, relies only on a stale snapshot price, or omits multi-year business outcomes, candor on uncertainty, or compounding runway.
- **0.0:** Ignores valuation, capital intensity, and long-term return implications completely.

## Facet: risk_buffers_and_competitive_reality [weight: 0.20] — Updates breaking-point buffers and industry value-chain shifts
- **1.0:** Re-tests the primary operational risk buffers after the print (loss-allowance coverage, capital headroom, and macro/FX sensitivity for lenders; retention floors, margin/SBC leverage, and rival moves in the value chain for software) rather than repeating static risk lists.
- **0.5:** Quotes updated balance-sheet or competitor facts without testing whether the stress buffer or value-chain advantage improved or narrowed.
- **0.0:** Omits updated risk buffers and competitive context.

## Facet: thesis_verdict_and_mbi_voice [weight: 0.10] — Honest thesis update and plain-spoken solo-investor prose
- **1.0:** Plain-spoken MBI prose delivering a calibrated update on what the quarter proved vs. left unproven (challenging overly rosy tracker labels if warranted), with zero leaked internal pipeline/file/JSON identifiers (such as `"the packet"` or raw schema field names), closing with concrete future checks for the empirical theses.
- **0.5:** Assigns a loose status update (`Intact`/`Watch`) without sharpening what remains unproven, slips into template prose, or leaks internal pipeline/schema jargon.
- **0.0:** Uncritical bull/bear spin or boilerplate clutter.

# Directive: LLM-driven peer selection (the generator behind the peer-comp panel)

**Status: SHIPPED 2026-06-13** (Chip 1, #530 generator + #523 consumer; `src/compute/peer_selection.py`). The spec body below is kept as the as-built record. Authored 2026-06-13 as the follow-on to S5 (`directives/interaction_paradigm_2026_06.md` row 2).

**2026-07-02 quality diagnosis + model decision (CLOSED):** the owner re-flagged
peers as still bad across most portfolio/evaluation names. Root cause was NOT the
generator or the model — it was the fundamentals-fetch seam: `_fetch_peer_fundamentals`
never ran on real builds (no `.env` FMP-key read, no per-file resume, and the
`income-statement` endpoint was missing so revenue never resolved), so every
LLM-suggested peer rendered all-em-dash and got dropped by the existing hide-don't-stub
filter. Fixed in #763 (`.env` key fallback, per-file resumable fetch, income-statement
endpoint, self-heal on cache-hit). That was the dominant failure mode.

Ran the eval's follow-up: thickened the golden set to 12 cases (`evals/golden/peer_selection.json`)
and swept Sonnet 4.6 / Opus 4.8 / Fable 5 on the current (v2) prompt plus a Sonnet/v1-prompt
regression check. All 4 arms unanimously missed `ps-veev-vertical-saas` — turned out to be a
golden-set bug (the original CRM/NOW/WDAY "expected" set was itself the sector-giant bias this
eval exists to catch; corrected to GWRE/NCNO/CERT, the real vertical-SaaS peer group, per
unanimous 4/4 model convergence on GWRE). Corrected recall:

| Arm | avg recall | wall (12 calls) |
|---|---|---|
| **Sonnet 4.6 / v2 (production)** | **0.972** | 999s |
| Opus 4.8 / v2 | 0.861 | 271s |
| Fable 5 / v2 | 0.972 | 258s |
| Sonnet 4.6 / v1 (prompt regression check) | 0.944 | 864s |

**Verdict: stay on Sonnet 4.6** (`LLM_MODELS["peer_selection"] = DEFAULT_MODEL`, unchanged).
Fable 5 ties Sonnet on recall but per the model-frontier reference prices *above* Opus
(~$15.7 blended $/MTok vs Sonnet's $4.71 — over 3x) — no parity-cost case to switch. Opus
is both worse on recall and pricier than Sonnet — ruled out. The v2 prompt (current
production) also beats the v1 regression check (0.972 vs 0.944), confirming the
CURRENTLY-listed / US-ticker rules added since v1 are a real improvement, not noise.

## Problem

The owner flagged the peer-comparison panel's companies as wrong ("these are shit
peers"). Everything shipped so far treats the *symptom*, not the *generator*:

- **PR7** scored the FMP screen and dropped all-em-dash rows (hide-don't-stub).
- **S5** (#512) made the panel *steerable* — `curate_peers` pins/excludes + a
  re-evaluable `peers_section_override` quality gate — but left the auto-selection
  unchanged.

The selection mechanism is still **100% the FMP sector/market-cap screen**:
`src/report/sections/p3_data.py::load_peer_comp` builds its pool from
`_fmp_peer_pool`, which reads `data/historical/fmp/{TICKER}_peers.json` (FMP's
`stock_peers` list, written by `execution/build_diligence.py`). It is then scored by
named-rival / industry / sector / scale affinity. That FMP list IS the thing the owner
called wrong (NU → Barclays, NOW → Applied Materials): an industry/cap screen has no
notion of *business-model comparability* (NU's real comps are MELI Credit, Inter,
StoneCo, Itaú's digital arm — not "diversified banks by market cap").

**There is no LLM-based peer selection anywhere in the codebase today** (verified
2026-06-13). This directive specifies it.

## Goal

Generate the comparable set with an LLM that actually understands the business, then
let the existing screen *corroborate* and supply metrics, and let the existing S5
human-curation + quality-override layers sit on top unchanged.

## Recommended approach — LLM proposes, FMP corroborates (augment, not replace)

This is the recommended default. (The simpler "LLM-only, drop the FMP screen" variant
is viable but loses FMP's scale/sector corroboration and the free metrics path; only
take it if the FMP pool proves to add no value in practice.)

1. **New LLM call: `peer_selection`.** Given the ticker, company name, business
   description / thesis lede, segments, and the FMP industry+sector as *hints* (not
   constraints), ask for the 6–10 best public-market comparables. Prompt must demand
   **business-model comparability**, not just sector — and allow cross-sector,
   foreign, and differently-sized comps where they're the true peers. Each returned
   peer carries a one-line `why` (the comparability rationale, which becomes the panel's
   existing `match_reasons` "why" column).
2. **Structured output (schema-validated).** Pydantic model
   `PeerSuggestion{ticker: str, name: str, why: str}` + a list wrapper. Validate at the
   call boundary; on parse failure, degrade to the FMP screen (never crash the build).
3. **Fetch fundamentals for suggested peers.** The panel's columns (mkt cap / revenue
   TTM / net margin / ROIC) come from cached FMP files (`_peer_revenue_ttm` etc. read
   `{peer}_key_metrics_ttm.json` + `{peer}_profile.json`). An LLM peer not already
   cached has no metrics and would render all-em-dash (then get dropped). So the
   pipeline must fetch each suggested peer's `profile` + `key_metrics_ttm` (reuse the
   FMP client `build_diligence.py` already uses; respect `FMP_TIER=free` rate limits —
   batch + cache). **This is the one non-trivial part; budget for it.**
4. **Merge into `load_peer_comp`, riding the S5 plumbing.** S5 already injects a chosen
   ticker absent from the FMP pool and scores it. LLM-suggested tickers become pool
   seeds the same way, with a new `match_reasons` tag (e.g. `"thesis peer"`). A peer in
   BOTH the LLM set and the FMP pool gets corroboration (rank it higher). FMP-only
   names rank below LLM-vouched ones.
5. **Caching.** Cache the suggestion set per ticker as an artifact (mirror the
   `bear_case` / `company_description` cache pattern under `data/` or `llm_artifact_store`),
   keyed on a hash of the inputs (name + business description + segments), invalidated on
   `--enable-llm --refresh`. Peer selection is stable quarter-to-quarter; do NOT call the
   LLM on every render — call on the LLM build, read the cache on render.
6. **Composition with S5 (must remain intact).** `competitive_watchlist` pins still get
   +3 and still inject; `peer_exclude` still drops; `peers_section_override`
   (`evaluate_peers_override`) still gates the whole panel. The owner's manual curation
   always wins over the LLM. Verify the S5 tests still pass.

## LLM governance (project rule — blocking)

Per `GEMINI.md` / the llm-evals rule, the new call needs all four:

- **Model-picker entry** in `src/llm/cli.py::LLM_MODELS` under purpose `peer_selection`
  (start at Sonnet/`DEFAULT_MODEL`).
- **Schema-validated structured output** (the Pydantic model above).
- **An eval** — a golden set of ~8–12 tickers with hand-picked "correct" peers
  (NU, NOW, MELI, UBER, etc., the names the owner has opinions about), scored by a
  brand-blind judge for overlap/precision. Wire into the existing evals harness +
  `--coverage`. This is how you decide Sonnet-vs-Opus empirically rather than guessing.
- **Cost/latency/failure logging** via the existing `source_calls` / LLM-budget ledger.

## Acceptance criteria

- For the owner's flagged names (NU, NOW, …), the panel's default peers are
  business-model comparables, not the FMP sector/cap head — verified on rebuilt reports.
  **CONFIRMED 2026-07-02** via `load_peer_comp(...)` for NU/NOW/VEEV post-#763: every row
  is tagged `thesis peer` with real business-model rationale (NU → SOFI/MELI/INTR/GRAB/
  KSPI/SE; NOW → WDAY/SNOW/CRM/DDOG/PLTR; VEEV → IQV/BR/VRSK/GWRE/TYL) — no FMP-only
  junk (Barclays / Applied Materials) surfaced. **Caveat**: the static workspace HTML only
  renders this panel for `--flavor evaluation` builds (`workspace_html.py`:
  `company_peers = p3.peer_comp if is_eval else None`) — portfolio-flavor rebuilds (NU,
  NOW, VEEV are all portfolio holdings) never show it in the static report. The only
  flavor-agnostic surface is the live `/api/peers/<ticker>` route (the Ask tool's
  "+ Peers" action) — confirmed end-to-end via a rebuilt ABNB (evaluation-flavor) report,
  which renders the full `<table>` correctly. Flagging for the owner: if portfolio-held
  names should also show this panel in their static report, that's a separate,
  intentional scope decision (UX gating, not a peer_selection bug) — not made here.
- Suggested peers render WITH computed multiples (the fetch step works), not em-dashes.
  **CONFIRMED**, with one documented limitation: FMP's `stable` `key-metrics-ttm` /
  `ratios-ttm` / `income-statement` endpoints return **HTTP 402** ("not available under
  your current subscription") for any symbol outside the plan's existing allowlist —
  `profile` (market cap) is the only endpoint that resolves for a genuinely-new peer
  ticker. This is a plan-tier ceiling, not a code defect: `_stable_get` already treats
  non-200/429 as a soft skip, and `load_peer_comp`'s `has_metrics` check (market cap OR
  revenue OR margin OR roic) means these partial peers still render (market cap + 3
  em-dash cells) instead of being dropped or showing a full em-dash wall — verified in
  the rebuilt ABNB report (TCOM row: mcap populated, rev/margin/roic show `—`). Peers
  already covered by the plan (SOFI, MELI, CRM, WDAY, GWRE, IQV, BKNG, UBER, …) render
  with all four columns populated.
- `curate_peers` pins/excludes and the `peers_section_override` quality gate still
  override the LLM set (S5 tests green). **CONFIRMED**: `test_peer_comp_scoring.py`,
  `test_peer_curation.py`, `test_peer_selection.py`, `test_ask_dock_and_peers.py` —
  53/53 pass.
- `peer_selection` eval lands ≥ the agreed precision bar; model choice justified by it.
  **CONFIRMED** — see the 2026-07-02 model-decision section above (Sonnet 4.6, 0.972
  corrected avg recall, cheapest at parity).
- Degrades to the FMP screen on any LLM/parse/fetch failure (build never crashes).
  Unchanged from ship — `extract_for_ticker` catches `StructuredParseError` and records
  `skipped_reason` rather than raising; `_fetch_peer_fundamentals` never raises on a
  fetch error (402 included) — both exercised live during this verification pass.
- No raw hex / new surface drift (the panel reuses existing markup; S1 guard stays green).

## Out of scope / notes

- This is the *generator*; S5 owns *steering* and the *quality gate* — don't re-litigate
  those.
- The `/api/peers/<ticker>` route (comments_server) reads `load_peer_comp`, so it inherits
  the improvement for free — no separate wiring.
- If `FMP_TIER` later upgrades, the suggested-peer fundamentals fetch gets cheaper; the
  design doesn't depend on it.

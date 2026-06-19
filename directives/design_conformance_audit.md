# Monthly design-conformance audit (the semantic backstop)

**Status:** runbook · cadence **monthly** · advisory (non-blocking).
Companion to `directives/design_language.md` and the executable guard
`tests/test_ui_controls.py`.

## Why this exists

Design drift is caught in **three layers**. The first two are deterministic and
block at merge; this audit is the third — the semantic net for what a regex
*cannot* see.

1. **Token guard** (`tests/test_ui_controls.py`, per-surface dimensions) — denies
   raw `#hex` **and** `rgb()/rgba()/hsl()/hsla()`, off-scale `font-size` /
   `border-radius`, non-token `font-family`, legacy aliases, `font-weight`
   700/800/900/bold, and `transition: all`. Opt-out: a new CSS-emitting surface
   must register and land clean (or quarantine with an owner).
2. **Component guards** — `kit-badge` (a reinvented FILLED status pill) and
   `test_buttons_compose_the_kit` (every emitted `<button>` carries a kit class
   `.k-btn`/`.k-chip`/`.k-prov-act` or an allowlisted bespoke control). These stop
   the two most common reinventions structurally.
3. **This audit** — an LLM review for the drift the above are blind to **by
   construction**, because the signature is legitimate elsewhere:
   - **accent / status color used as decoration** (a `border-left` rail, a
     colored heading) rather than for interactive / selected / unread / value-status;
   - **mono on a label / heading / button** (mono is tickers / numbers / code /
     timestamps / locators only) — `var(--mono)` is a valid token, so the guard
     can't tell it's on the wrong element;
   - **font-size hierarchy inversions** — the same role rendered at two sizes
     across surfaces, or a clicked-into / drawer element rendering *smaller* than
     its trigger (every value can be on-scale yet still wrong);
   - **missing panel anatomy** — a flat border-box where the workspace uses
     `.panel-head` / hairline / `.panel-foot`; a floating KPI strip instead of the
     gridline-gap pattern;
   - **reinvented outline chips / tags** the `kit-badge` regex can't catch (it
     only fires on filled status pills).

## Method (what the scheduled run does)

1. Read `directives/design_language.md` (the rulebook) + `src/ui/controls.py` +
   `src/ui/tokens.py` (the kit + values) — this is the gold-standard DNA.
2. Review the dashboard / pipeline surfaces (`src/pipeline/*.py`,
   `src/dashboard/*.py`) against that DNA for the five semantic drift classes
   above. **Do NOT** re-flag what the deterministic guards already enforce, and
   **do NOT** touch the workspace report (`src/report/**`) — its editorial type
   ramp is a sanctioned §1 exception.
3. **Adversarially verify every finding against current code before reporting it**
   — prior whole-app scans over-counted ~2× (stale, already-fixed, or sanctioned
   "defensible keeps": mono on a ticker SYMBOL is correct; status `border-left`
   rails using `--ok/--warn/--bad` for *status* are §2-correct; the `.src-chip`
   8.5px mark, `0.93em` inline mono, and the bespoke `.qbtn`/`.ic-btn`/
   `.twk-toggle-btn` controls are sanctioned).
4. Write a dated report to `data/design_audit/<YYYY-MM-DD>.md` (gitignored). If
   confirmed drift exists, either open a small PR composing the kit, or append a
   `directives/data_fixes.md` entry — never auto-merge a visual change unreviewed.

The thorough form is the multi-agent fleet used in the 2026-06-19 sweep
(extract DNA → per-cluster review → adversarial verify → synthesize); a lighter
single-pass review is fine for the routine monthly check. Either way, the output
is a *report*, not a silent edit.

## Scheduling

Registered as a monthly recurring agent (see `/schedule list`). To change cadence
or pause: `/schedule` (cloud routine) or the machine's scheduled-tasks list. The
deterministic guards (layers 1–2) are the real gate; this audit is the advisory
catch for taste-level drift, so a missed month degrades gracefully.

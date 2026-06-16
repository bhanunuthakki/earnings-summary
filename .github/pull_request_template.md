<!-- Keep this short. Delete sections that don't apply. -->

## What & why

<!-- One or two lines: what this changes and the reason. -->

## Frontend conformance (delete if this PR renders nothing)

If this PR adds or changes **anything rendered to the frontend** (a row, section,
panel, badge, button, chip, callout, ticker, prose block), it MUST compose the
control kit — never freehand CSS. See `directives/design_language.md` §4 / §7.1.

- [ ] Buttons use `.k-btn` (`-primary` / `-quiet` / `-danger`, `-sm` for dense) — no freehand `<button>` skin (background/border/color/padding).
- [ ] Filled status badge uses `.k-pill` (+ `-ok/-warn/-bad`) — **not** a hand-rolled `.*-pill`/`.*-badge` with a `color-mix(var(--ok|warn|bad))` background (the `kit-badge` guard fails this).
- [ ] Outline kind/filter tag uses `.k-chip` (+ tones / `-mono` / `-btn`); a callout block uses `.k-well`; a ticker+name uses `ticker_label()`; stored prose/notes use `ui.prose.render_prose`.
- [ ] Local CSS is **layout only** (width/flex/grid/gap); JS-hook classes kept *alongside* the kit class, not replaced.
- [ ] A new `var(--`-emitting `src/**.py` is added to `REGISTERED` in `tests/test_ui_controls.py` and is token-clean.
- [ ] `python -m pytest tests/test_ui_controls.py -q` passes. If a report renderer changed: regenerated workspace goldens (`GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py`) and reviewed the diff.

## Verification

<!-- Tests run / how you confirmed it works. -->

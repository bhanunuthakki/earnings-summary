# Brief Library Scope 1 — approved global requirements

**Status:** owner-approved mockup direction as of 2026-08-29; canonical contracts, production implementation, and local rendered browser acceptance are complete; Windows activation/live hydration remains recorded in `docs/design/brief_library_scope_1_implementation_roadmap_2026_08_29.md`.
**Approval boundary:** reconcile these requirements through `DEFINITIONS.md`, the shared UI masters, the design registry, and every adopting surface rather than patching Brief Library locally.

## Vocabulary to ratify

- **Full Research Brief** is the candidate canonical artifact term behind the short UI label **Brief**. It needs a project definition before `Brief` becomes a durable cross-surface label because **Pre-Earnings Brief** already has a distinct canonical meaning.
- **Pre-Earnings Brief** remains the canonical term; its compact library title and chip label are **Pre-Earnings**.
- **Post-Earnings Readout** remains the canonical term; its compact library title and chip label are **Post-Earnings**.
- **Coverage Role** remains the canonical persisted relationship vocabulary. The approved Brief Library states use **Portfolio** and **Evaluation**; any additional roles admitted to the library must preserve their exact canonical meaning rather than being collapsed into either label.
- The compact artifact title contract is `[TICKER] [Qn yy] [Brief | Pre-Earnings | Post-Earnings]`, for example `BKNG Q2 26 Post-Earnings`.

## Global interaction and design requirements

1. Dropdown triggers and their option lists are one app-owned control family. Do not rely on browser/OS-native `<select>` popovers where their ground, border, type, focus, and selected state can diverge from the application theme.
2. App-owned dropdown lists use the shared surface, border, text, focus, and elevation roles in both themes. The trigger and listbox are keyboard operable: open, close/Escape, ArrowUp/ArrowDown, Enter/Space selection, and focus return.
3. Table and library facets are mutually dependent. Every option set and count recomputes from the selections in the other facets; an incompatible retained value clears deterministically.
4. Every dropdown uses one automatic typeahead experience across the program. A printable search character filters the dropdown that is open or whose trigger has focus; a surface may nominate one default dropdown (Brief Library nominates Company) to claim typing when no dropdown is focused. As soon as typing begins, the active trigger replaces its prior `All … · count` or selected-option label with the live typeahead buffer itself—without a `Search:` prefix, count, icon, visible search field, or separate mode switch. Backspace edits both the visible buffer and results; Escape or outside dismissal restores the prior selected/all label; ArrowUp/ArrowDown and Enter complete a keyboard-only selection.
5. Artifact-kind, Coverage Role, freshness, and availability chips use registered semantic variants. Colors communicate the closed category mapping consistently across surfaces; consumers do not invent local chip colors.
6. The fiscal-period end-date phrase is omitted from compact library rows when the quarter identity is already present in the title.
7. Compact titles wrap naturally and may use the row's vertical space. They are never ellipsized into an ambiguous artifact identity.

## Production seams after mockup approval

- Vocabulary: `DEFINITIONS.md`.
- Control behavior and visuals: `src/ui/controls.py`, `src/ui/design_registry.py`, and the owning family master.
- Brief Library composition and behavior: `src/pipeline/work_os_research.py`, `src/pipeline/work_os_styles.py`, and `src/pipeline/work_os_shell.py`.
- Adoption: inventory every native select, ticker picker, artifact title, and category chip before changing shared behavior; migrate through the registered control rather than duplicating per-surface implementations.

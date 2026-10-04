# Design artifact lifecycle

Reviewed 2026-10-03 against committed main `e05c3457` and the wider cleanup assessment.
This inventory records existing ownership. It does not approve new product behavior,
remote publication, deletion of source evidence or an application redesign.

| Artifact group | Current lifecycle and owner | Retained requirement or acceptance boundary |
|---|---|---|
| `mockups/harvey_sidebar_flow.html` | Active runtime markup master; `src/pipeline/work_os_shell.py`, design-sync gate | Work OS loads this file. Retain loader, protected transforms, shared assets and control tests. A future template move requires integrated loader/gate/reconstruction and browser proof. |
| `mockups/explore_hybrid_2_analysis_sheet.html`, `explore_hybrid_2_workbench_interactive.html`, `explore_concepts.css`, `explore_metric_catalog.js` | Tested Explore reference; `tests/test_explore_mockup.py` and production `explore_panel.py` | Preserve one progressive analysis/workbench flow, shared catalog and linked assets. Prototype promises do not certify current financial evidence. |
| `mockups/explore_hybrid_2_fullscreen_workbench.html` | Historical alternative, retained in Git | Superseded composition links to the retained analysis sheet. Keep recoverable history until its interaction differences are mapped; do not treat it as current design authority. |
| `mockups/explore_analysis_notebook.html`, `explore_pivot_studio.html`, `explore_query_canvas.html`, `explore_hybrid_1_conversational_pivot.html`, `explore_hybrid_3_metric_story.html` | Unselected alternatives, retained | Different interaction hypotheses remain unratified. Current Explore is the production owner. Preserve ideas before a later archive; no new feature commitment follows from these files. |
| `mockups/explore_emergent_1_artifact.html`, `explore_emergent_2_spotlight.html`, `explore_emergent_3_trail.html` | Unselected alternatives, retained | Preserve distinct artifact, search and trail concepts until a product decision resolves them. |
| `mockups/explore_readonly_dcf_1_peek.html`, `explore_readonly_dcf_2_modes.html`, `explore_readonly_dcf_3_overlay.html`, `explore_readonly_dcf_3_rich.html`, `explore_readonly_dcf.css`, `explore_guided_demo.html`, `explore_dcf_linking_proposal.md` | Linked exploratory family, retained | Keep family links and CSS together. The accepted DCF reader and write boundary own numerical and artifact truth; this proposal does not activate a forecast reader. |
| `mockups/company_desk_mockup.html` | Approval/implementation reference; production `work_os_research.py` | Production composition has independent approval tests. Prototype chronology, dialog accessibility and roving tab assertions need a complete mapping before retirement. |
| `mockups/portfolio_copilot_mockup.html`, `performance_risk_mockup.html` | Tested historical references | Preserve quick-response, policy, correlation and truth-label assertions until their production replacements are demonstrated. |
| `mockups/company_research_experience.html`, `copilot_conversation_prototype.html`, `evaluation_investment_profile_mockup.html` | Tested future contracts, retained | Thesis review, exact-diff approval and investor-profile ratification remain explicit requirements; matching production names do not prove complete adoption. |
| `design-system/`, `.design-sync/` | Supported prototyping package; generated kit assets | Python `src/ui/` owns tokens and controls. Current README/generators replace historical hand-port guidance. No upload is part of this cleanup. |
| `explore-sandbox/` | Supported optional analytics surface | Explicit synthetic or provenance-bearing restored database only. Generated theme and optional dependencies remain governed together. |
| Ignored early IA drafts, screenshots and saved fieldbook/report exports | Private owner artifacts, retained outside tracked cleanup | Ignored status is not a retirement decision. Preserve source/privacy class, raw reports, approval notes and recovery before any later move. Git alone cannot recover ignored files. |

The review found no unconditional mockup deletion candidate. Retention closes this code
cleanup's lifecycle decision without claiming unresolved feature choices are approved.
The next archive boundary needs a requirement-to-production map and recoverable location.
Product acceptance belongs to the owner; runtime acceptance belongs to the existing
production tests, design gate and rendered user-path evidence.

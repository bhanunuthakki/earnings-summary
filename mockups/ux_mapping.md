# Equity Research Platform — UX & Screen Architecture Map

This document specifies the complete Information Architecture (IA) and UX layout by screen count for the equity research platform.

---

## 1. UX Structure & Screen Count Summary

The application is structured into **7 core screens (consoles/workspaces)** and **4 global transient overlays**.

| Screen # | Screen Name | Route / Anchor | Primary Purpose | Key Sub-views / Panels |
|---|---|---|---|---|
| **1** | **Today Console** | `#home` (`/`) | Daily cockpit, active tasks, open loops, and urgent alerts | Open Loops, Continue Where Left Off, Senior Partner Brief, Catalysts, Recent Activity |
| **2** | **Companies Hub** | `#companies` | Universe overview, watchlists, and research stream | Holdings (active names), Discovery (peers/pipeline), Diet (signals pull stream) |
| **3** | **Company Workspace** | `/workspace/{ticker}` | Deep-dive equity research per ticker | Thesis & Falsifiers, Financials & DCF, Transcripts & Say/Do, Notes & Provenance |
| **4** | **Portfolio Consoles** | `#portfolio` | Capital allocation, risk budget, and audit log | Health Console, Allocation Console (Next-Dollar), Record Console (Decisions log) |
| **5** | **Review & Rituals** | `#review` | Weekly ritual completion & thought processing | Musings (unprocessed thoughts), Triage (unmapped comments), Journal (decision grading) |
| **6** | **Ask Engine** | `#explore` (`#ask`) | Natural language research & ViewSpec analytics | NL Query Builder, Multi-series Charting Canvas, Saved Views Library |
| **7** | **Mobile Inbox** | `/mobile/inbox` | Tailscale-protected phone companion | Decision Draft Confirmations, Senior Partner Brief, Quick Verdicts |

---

## 2. Detailed Screen Descriptions

### Screen 1: Today / Home Console (`#home`)
- **Role**: Operational launchpad. Answers "What needs my attention right now?" and "Where did I leave off?".
- **Structure**:
  - **Header Bar**: Global Nav, Search (`Ctrl+K`), Fast Capture (`Ctrl+M`), System Health Tick.
  - **Open Loops Action Pack**: High-urgency cards (Unconfirmed decision drafts, missing falsifiers, pending reconcile verdicts).
  - **Continue Where You Left Off**: Quick-resume doorways to recent workspaces and DCF models with last-visited timestamps.
  - **Senior Partner Brief**: Strategic executive summary, portfolio posture, and macro/earnings context.
  - **Upcoming Catalysts Rail**: 14-day calendar of earnings releases, SEC filings, and macro dates.
  - **Signals & Feed Stream**: Non-decaying information feed filtered by high thesis relevance.

### Screen 2: Companies Hub (`#companies`)
- **Role**: Directory and universe view for all monitored equities.
- **Structure**:
  - **Holdings Sub-tab**: Grid/table of active portfolio positions with thesis status badges (`.k-pill-ok`, `.k-pill-warn`), target prices, and allocation weights.
  - **Discovery Sub-tab**: Pipeline of prospective companies, peer groups, and watchlist tickers.
  - **Diet Sub-tab**: Stream of IR press releases, analyst rating shifts, and transcript drops.

### Screen 3: Company Workspace (`/workspace/{ticker}`)
- **Role**: The core single-stock research workspace.
- **Structure**:
  - **Identity Header**: Ticker, company name, live price, market cap, and thesis conviction badge.
  - **Tab 1 — Thesis & Business**: Core investment thesis, key value drivers, disconfirming evidence / falsifiers, management say/do tracking.
  - **Tab 2 — Financials & DCF**: Interactive financial statements (Income Statement, Balance Sheet, Cash Flow), segment revenue breakdown, embedded DCF model link/recomputing engine.
  - **Tab 3 — Transcripts & QA**: Transcripts list, Q&A extractions, key management quotes with provenance chips.
  - **Tab 4 — Notes & Provenance**: Analyst notes stream, external documents (10-K, 10-Q, investor decks), audit log.

### Screen 4: Portfolio Consoles (`#portfolio`)
- **Role**: Portfolio construction, risk budgeting, and investment governance.
- **Structure**:
  - **Health Console**: Thesis health aggregation, sector/factor concentration, Red Team disconfirming risks.
  - **Allocation Console**: Governed Next-Dollar capital allocation recommendation engine, risk budget utilization, position sizing guidance.
  - **Record Console**: Full historical audit log of every investment decision, decision memo, and price trigger.

### Screen 5: Review & Rituals (`#review`)
- **Role**: Ritual execution space matching the weekly research workflow.
- **Structure**:
  - **Musings Landing**: Raw captured thoughts, voice memos, quick notes waiting for categorization.
  - **Triage Queue**: Low-confidence LLM extractions, unmapped user comments, data validation warnings.
  - **Journal & Beliefs**: Matured decision outcome grading, belief update log, disconfirmation checks.

### Screen 6: Ask Engine (`#explore` / `#ask`)
- **Role**: Quantitative research & natural language query canvas.
- **Structure**:
  - **Query Input**: NL prompt box with auto-suggest chips (`"Show net margin for NU vs BKNG, last 8 quarters"`).
  - **Chart Canvas**: ViewSpec rendering engine supporting multi-series line charts, bar comparisons, and table views.
  - **Saved Views**: Library of bookmarkable queries and saved charts.

### Screen 7: Mobile Inbox (`/mobile/inbox`)
- **Role**: Phone-optimized interface for quick actions and review on the go.
- **Structure**:
  - **Action Cards**: One-tap Ratify / Rewrite / Drop / Defer controls for decision drafts.
  - **Senior Partner Brief**: Mobile-optimized text view of the daily brief.

---

## 3. Global Transient Overlays (`window.CCOverlay`)

1. **Command Palette (`Ctrl+K`)**: Fast modal overlay for quick-jumping to any ticker, workspace, panel, or executing CLI actions.
2. **Ask Copilot Dock**: Side drawer allowing conversational LLM analysis alongside any active workspace without switching context.
3. **Provenance Peek Drawer**: Slide-over panel displaying original source documents, SEC filing extracts, and transcript audio timestamps (`data-peek-url`).
4. **Fast Capture Modal (`Ctrl+M`)**: Compact modal for quick-entry musings and thoughts.

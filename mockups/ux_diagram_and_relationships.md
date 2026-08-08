# 2-Surface Unified Portfolio Cockpit Architecture Diagram

This document outlines the retrenched **2-Surface Unified Portfolio Cockpit Architecture** for the equity research platform based on collapsing Today, Portfolio, and Review into ONE unified home cockpit.

---

## 1. The 2 Core Surfaces

1. **Surface 1: Portfolio Cockpit & Review (`#home` / `/`)**:
   - **Hero Band**: Portfolio Thesis Health (88% Health, +32.4% 1Y Return), Risk Budget, and Governed Next-Dollar Allocation Target (`NU - $1,500`).
   - **Review Queue Band**: Pending Decision Drafts (Ratify/Defer), Missing Falsifier Alerts, Reconcile Queue.
   - **Holdings & Watchlist Directory**: Active portfolio positions & watchlist equities with conviction pills, target prices, and 1Y returns. Clicking any row opens its Company Workspace.

2. **Surface 2: Company Research Workspace (`/workspace/{ticker}`)**:
   - High-density single-page editorial research desk for any ticker (`NU`, `BKNG`, `TSM`).
   - All supporting modules open as **Slide-Over Drawers**:
     - 📊 **Financial Statements & DCF Model Drawer**
     - 🎙️ **Management Say/Do & Transcripts Drawer**
     - ⚔️ **Peer Comparison Matrix Drawer**
     - ⚠️ **Disconfirming Falsifiers Contract Drawer**
     - 📜 **Raw SEC Filing Citation Drawer**
     - 💬 **Ask Copilot Side Dock**

---

## 2. 2-Surface Architecture Topology

```mermaid
flowchart TD
    subgraph RootShell["Command Center Shell (2 Core Handles)"]
        S1["1. Portfolio Cockpit & Review (#home)"]
        S2["2. Company Research Desk (/workspace/ticker)"]
    end

    subgraph SlideOverDrawers["Contextual Slide-Over Drawers (Slide from Right, Preserve Active Desk)"]
        D_Fin["📊 Financial Statements & DCF Model Drawer"]
        D_SayDo["🎙️ Management Say/Do & Transcript Verification Drawer"]
        D_Peers["⚔️ Peer Group Comparison Matrix Drawer (SE, MELI)"]
        D_Falsifier["⚠️ Disconfirming Falsifiers Contract Drawer"]
        D_Copilot["💬 Ask Copilot Research Assistant Dock"]
        D_Peek["📜 Raw SEC Filing & Provenance Citation Peek"]
    end

    subgraph Surface1_Content["Surface 1: Portfolio Cockpit (#home)"]
        S1_Target["Governed Next-Dollar Allocation Target ($1,500 NU)"]
        S1_Health["Portfolio Thesis Health Rollup (88% Health)"]
        S1_Queue["Review Queue & Action Pack (Decision Drafts)"]
        S1_Holdings["Holdings & Watchlist Directory Table"]
    end

    subgraph Surface2_Content["Surface 2: Company Research Desk (/workspace/ticker)"]
        S2_Thesis["Investment Thesis & Core Value Drivers"]
        S2_Metrics["Core Metric Table (Revenue, Net Income, ARPAC)"]
        S2_Falsifiers["Active Falsifiers Summary Card"]
        S2_Peers["Peer Ticker Handles (SE, MELI)"]
    end

    %% Connections
    S1 --> Surface1_Content
    S2 --> Surface2_Content

    %% Cross-Surface Doorways
    S1_Queue -- "Ratify / Defer" --> S1_Target
    S1_Holdings -- "Click Ticker Row" --> S2

    %% Contextual Drawer Launches
    S2_Metrics -- "Click Figure" --> D_Fin
    S2_Thesis -- "Click Quote" --> D_SayDo
    S2_Peers -- "Click Peer Ticker" --> D_Peers
    S2_Falsifiers -- "Click Threshold" --> D_Falsifier
    S2_Thesis -- "Click Citation Tag" --> D_Peek
    S2 -- "Click Copilot" --> D_Copilot
```

---

## 3. Retrenchment Summary

| Former View | Retrenchment & New Home |
|---|---|
| **Today Page** | **COLLAPSED into Home Cockpit**: Open loops sit directly below Portfolio Health on `#home`. |
| **Portfolio Page** | **PROMOTED to Home Cockpit**: Portfolio Health & Governed Next-Dollar allocation target become the main hero of the app. |
| **Companies Page** | **EMBEDDED into Home Cockpit**: Active holdings and watchlist live directly on `#home` below the Review Queue. |
| **Review Page** | **EMBEDDED into Home Cockpit**: Synchronized with Telegram Sunday Packet; ratifications execute directly on `#home`. |
| **Ask Engine** | **DEMOTED to Slide Dock**: Slides out from right edge on demand (`Ask Copilot 💬`). |

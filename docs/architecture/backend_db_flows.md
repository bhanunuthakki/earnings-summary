# Backend and database flows

This map describes the localhost application after the August 2026 debt-hardening pass. It groups the 154 Flask routes by responsibility while preserving the distinct ingress, managed-runtime, orchestration, persistence, cache, and external-provider boundaries.

## Runtime and backend flow

```mermaid
flowchart TB
    subgraph ingress["Ingress and triggers"]
        browser["Browser UI\nlocalhost or approved Tailscale client"]
        telegram["Telegram long poll\ntext, voice, documents, callbacks"]
        scheduler["Windows Task Scheduler\n15 daily, 22 weekly, 5 monthly, 1 hourly, 1 logon"]
        cli["Operator CLI entrypoints"]
    end
    subgraph boundary["Trust and request boundary"]
        network["Network allowlist\nloopback plus optional Tailscale"]
        origin["Origin and capability checks\nstate-changing HTTP requests"]
        tg_allow["Preconfigured Telegram chat allowlist\nmissing, invalid, or unreadable means reject"]
        runlock["Shared run and DB write-set locks"]
        child_builder["Canonical managed Python child argv\nrepository target validation"]
        sqlite_boot["Verified SQLite 3.53.4 bootstrap\none hash/load per process"]
    end
    subgraph flask["Flask cockpit :7421"]
        routes["154 routes\ncore 111, DCF 7, content 24, alerts 6, settings 4, journal 2"]
        panels["Panel fragment renderer\n30-second, 256-entry cache plus ETag/304\nper-key single-flight"]
        telemetry["Panel latency telemetry\nobservational POST does not evict cache"]
        ask["Grounded Ask orchestration\nSQL-first numeric views; lexical narrative evidence\nappend-only retrieval traces"]
        jobs["Background job registry\nsingle-flight, at most 3 subprocesses, SSE status"]
        reports["Company brief builder\none borrowed read connection to HTML, Markdown, JSON"]
    end
    subgraph orchestration["Deterministic orchestration"]
        morning["Typed 20-stage morning manifest at 04:00\nday + manifest-scoped atomic 18-hour checkpoint"]
        capture["Capture routing\ntext, transcription, documents, callbacks"]
        fetch["Market, filing, IR, transcript, and portfolio fetchers"]
        normalize["Typed validation and normalization\n10% batch drop-ratio drift gate"]
        compute["Deterministic compute\nDCF, KPIs, risk, signals, decisions"]
        artifact_build["Brief and recommendation artifact builders"]
        notifications["Pull surfaces and Telegram response builders"]
        http["Canonical pooled HTTP client\ntimeouts, idempotent retries, host budgets"]
    end
    subgraph llm["Governed LLM path"]
        purpose["Closed purpose plus prompt version"]
        budget["Pre-call monthly budget check"]
        resolver["Purpose-based model and provider resolver"]
        provider["Codex or Claude subscription\nallowlisted OpenRouter fallback\nexplicit Gemini backend purposes"]
        schema["Schema validation and bounded repair"]
        ledger["Attempt ledger\nprovider, model, tokens, public-list cost, latency, fallback"]
        artifact_cache["Artifact cache\ninput hash plus purpose TTL plus dirty invalidation"]
    end
    subgraph storage["Local durable state"]
        sqlite["data/portfolio.db\ncanonical SQLite database"]
        temp[".tmp checkpoints and intermediate payloads"]
        cache["Bounded process and response caches\n48-hour schema/versioned materializations"]
        outputs["Rendered briefs and operator-visible artifacts"]
        retry_receipts["Atomic retry receipts\nfor committed DCF changes awaiting lineage"]
    end
    subgraph external["External read providers"]
        fmp["FMP"]
        sec["SEC EDGAR and XBRL"]
        yahoo["Yahoo market data"]
        ir["Company IR and transcript sources"]
        media["YouTube and Whisper"]
        portfolio["Portfolio Tracker and Wealthplan"]
        drive["Google Drive and Sheets"]
    end

    browser --> network --> routes
    routes --> origin
    telegram --> tg_allow --> capture
    scheduler --> child_builder
    cli --> child_builder
    child_builder --> sqlite_boot --> runlock --> morning
    routes --> panels
    panels --> telemetry
    routes --> ask
    routes --> jobs --> child_builder
    routes --> reports
    runlock --> orchestration
    morning --> fetch --> normalize --> compute --> artifact_build
    fetch --> http
    capture --> normalize
    ask --> purpose
    artifact_build --> purpose
    purpose --> budget --> resolver --> provider --> schema --> ledger
    schema --> artifact_cache
    artifact_cache --> reports
    artifact_cache --> notifications
    fmp --> http
    sec --> http
    yahoo --> http
    ir --> http
    media --> capture
    portfolio --> fetch
    drive --> fetch
    normalize --> sqlite
    compute <--> sqlite
    ledger --> sqlite
    artifact_cache <--> sqlite
    panels <--> cache
    reports --> outputs
    morning <--> temp
    compute -. "lineage retry when post-write receipt fails" .-> retry_receipts
    reports --> browser
    notifications --> telegram
```

## Database access, lineage, and publication flow

```mermaid
flowchart LR
    path["Canonical DB path resolver\nCLI override, then environment, then repo default"] --> runtime["connect_sqlite\nwriter runtime version gate"]
    subgraph roles["Connection roles"]
        ro["READ_ONLY\nquery-only plus schema preflight"]
        writer["WRITER\nWAL, FK ON, busy timeout, synchronous NORMAL"]
        snapshot["SNAPSHOT\nconsistent backup and export reads"]
    end
    runtime --> ro
    runtime --> writer
    runtime --> snapshot
    subgraph request["Request and report ownership"]
        flask_g["Flask g.request_read_db\nexact create_app-injected DB path"]
        teardown["teardown_request closes owner"]
        report_conn["One borrowed report connection"]
        sections["Description, financials, evaluation, compensation, synthesis, decision card, provenance"]
    end
    ro --> flask_g --> report_conn --> sections
    flask_g --> teardown
    subgraph ingestion["Ingestion and evidence"]
        raw["Documents, filings, transcripts, prices, portfolio snapshots"]
        validate["Pydantic contracts, currency and period checks, plausibility gates"]
        evidence["Source evidence and provenance records"]
        facts["Fact cells, financial facts, KPI observations, derivations"]
        resolution["Restatement and conflict resolution"]
    end
    raw --> validate --> evidence --> facts --> resolution
    writer --> raw
    writer --> evidence
    writer --> facts
    subgraph research["Research state"]
        thesis["Thesis state and tracked KPI registry"]
        decisions["Decision journal, drafts, nudges, process quality"]
        signals["Risk, valuation, scenario, alert, and say-do signals"]
        artifacts["LLM artifacts and call ledger"]
        publication["Current brief snapshots and published report sections"]
    end
    resolution --> thesis
    resolution --> signals
    thesis --> decisions
    signals --> decisions
    thesis --> artifacts
    decisions --> artifacts
    artifacts --> publication
    sections --> publication
    subgraph retrieval["Search and Ask retrieval"]
        views["Facts & Analytics / numeric Ask\nvalidated ViewSpec over canonical fact tables\nperiod, unit, and source per cell"]
        operational["Default narrative Ask\nticker-filtered SQL facts plus lexical filings/transcripts\ntool-free, spotlighted evidence"]
        op_trace["Operational grounding trace\nquestion hash, scope, locators, evidence hashes\nanswer FK and durable replay; no passage duplication"]
        evidence_cache["Per-session evidence cache\nscoped fact/document revision token\nchat and audit writes do not evict"]
        corpus["Search corpus projection"]
        fts["SQLite FTS5"]
        vectors["Optional Lance vector projection"]
        sealed["Optional promoted sealed retrieval\nnot required by the product default"]
        audit["Ask session, answer, and citation audit"]
    end
    facts --> views --> op_trace
    evidence --> evidence_cache --> operational --> op_trace
    facts --> operational
    op_trace --> audit
    evidence --> corpus
    facts --> corpus
    corpus --> fts
    corpus --> vectors
    fts --> sealed
    vectors --> sealed
    sealed --> audit
    audit --> artifacts
```

## Schema upgrade, backup, retention, and deletion flow

```mermaid
flowchart TB
    start["Database presented for upgrade"] --> runtime_gate["Require WAL-reset-safe SQLite runtime"]
    runtime_gate --> lock["Acquire shared write lock"]
    lock --> classify{"Revision and schema classification under lock"}
    classify -->|"Fresh or active"| active["Active Alembic graph\n0001 initial schema through 0014 Ask grounding traces"]
    classify -->|"Recognized archived revision"| bridge["Archived upgrade bridge"]
    classify -->|"Unknown or incompatible"| refuse["Fail closed\nno mutation"]
    bridge --> backup["SQLite online backup\nrefuse overwrite"]
    backup --> precheck["Validate legacy schema and revision"]
    precheck --> archived["Replay archived graph to archived head"]
    archived --> validate["Validate expected schema"]
    validate --> reanchor["Re-anchor to active 0001"]
    reanchor --> active
    active --> integrity["Verify active head 0014\nPRAGMA integrity_check plus foreign_key_check"]
    integrity --> ready["Runtime-ready database"]
    ready --> scheduled_backup["Scheduled online backup"]
    scheduled_backup --> encrypt["gzip plus AES-256-GCM"]
    encrypt --> receipt["Verify this run's encrypted .gz.enc receipt and file"]
    receipt --> sibling["Sibling restore set and restore drill"]
    receipt --> gc["GC planner\nscheduled --apply only after verified backup\nallowlist-only"]
    gc --> archive_first["Archive-first eligible rows"]
    archive_first --> delete["Guarded deletion after verification"]
    gc --> blocked["facts-depth destructive cleanup blocked\nuntil immutable archive receipt exists"]
    ready --> deletion_eval["Deletion catalog evaluator"]
    deletion_eval --> targets["6 governed candidates, 25 schema targets\ncode targets Git-restorable"]
    targets --> import_scan["Absolute and relative import reachability scan\nparse/read errors fail closed"]
    import_scan --> eligibility{"Eligible?"}
    eligibility -->|"No"| refuse_delete["Keep code and schema"]
    eligibility -->|"Yes"| governed_delete["Apply cataloged deletion"]
    backup -. "Production-derived pre-0002 recovery source verified" .-> sibling
```

## Operational boundaries

- The application is single-user and localhost/Tailscale-scoped; it is not a public multi-tenant service.
- Live scheduler registration, optional provider credentials, and real production population are operational checks, not implied by source-level green tests.
- The production-derived pre-`0002` backup has passed integrity, foreign-key, bridge, and deleted-plane recovery verification; Git still restores code rather than database rows.
- The dormant legacy Gemini fallback compatibility exports are outside the canonical production route and remain one low-priority cleanup item pending an external-import compatibility scan.

# Backend and database flows

This map describes the localhost application after the August 2026 debt-hardening pass. It groups the 153 Flask routes by responsibility while preserving the distinct ingress, orchestration, persistence, cache, and external-provider boundaries.

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
    end
    subgraph flask["Flask cockpit :7421"]
        routes["153 routes\ncore 117, content 24, alerts 6, settings 4, journal 2"]
        panels["Panel fragment renderer\n30-second, 256-entry cache plus ETag/304"]
        telemetry["Panel latency telemetry\nobservational POST does not evict cache"]
        ask["Ask orchestration\n4-worker pool, bounded pack and artifact reads"]
        jobs["Background job registry\nsingle-flight, at most 3 subprocesses, SSE status"]
        reports["Company brief builder\none borrowed read connection to HTML, Markdown, JSON"]
    end
    subgraph orchestration["Deterministic orchestration"]
        morning["Morning pipeline at 04:00\n18-hour resumable checkpoint"]
        capture["Capture routing\ntext, transcription, documents, callbacks"]
        fetch["Market, filing, IR, transcript, and portfolio fetchers"]
        normalize["Typed validation, plausibility checks, normalization"]
        compute["Deterministic compute\nDCF, KPIs, risk, signals, decisions"]
        artifact_build["Brief and recommendation artifact builders"]
        notifications["Pull surfaces and Telegram response builders"]
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
        cache["Bounded process and response caches"]
        outputs["Rendered briefs and operator-visible artifacts"]
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
    scheduler --> runlock --> morning
    cli --> runlock
    routes --> panels
    panels --> telemetry
    routes --> ask
    routes --> jobs
    routes --> reports
    jobs --> orchestration
    morning --> fetch --> normalize --> compute --> artifact_build
    capture --> normalize
    ask --> purpose
    artifact_build --> purpose
    purpose --> budget --> resolver --> provider --> schema --> ledger
    schema --> artifact_cache
    artifact_cache --> reports
    artifact_cache --> notifications
    fmp --> fetch
    sec --> fetch
    yahoo --> fetch
    ir --> fetch
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
    reports --> browser
    notifications --> telegram
```

## Database access, lineage, and publication flow

```mermaid
flowchart LR
    path["Canonical DB path resolver"] --> runtime["connect_sqlite"]
    subgraph roles["Connection roles"]
        ro["READ_ONLY\nquery-only plus schema preflight"]
        writer["WRITER\nWAL, FK ON, busy timeout, synchronous NORMAL"]
        snapshot["SNAPSHOT\nconsistent backup and export reads"]
    end
    runtime --> ro
    runtime --> writer
    runtime --> snapshot
    subgraph request["Request and report ownership"]
        flask_g["Flask g.request_read_db"]
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
        corpus["Search corpus projection"]
        fts["SQLite FTS5"]
        vectors["Optional Lance vector projection"]
        sealed["Sealed retrieval pack with citations"]
        audit["Ask session, answer, and citation audit"]
    end
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
    start["Database presented for upgrade"] --> classify{"Revision and schema classification"}
    classify -->|"Fresh or active"| active["Active Alembic graph\n0001 initial schema → 0002 debt cleanup → 0003 baseline repair"]
    classify -->|"Recognized archived revision"| bridge["Archived upgrade bridge"]
    classify -->|"Unknown or incompatible"| refuse["Fail closed\nno mutation"]
    bridge --> lock["Acquire shared write lock"]
    lock --> backup["SQLite online backup\nrefuse overwrite"]
    backup --> precheck["Validate legacy schema and revision"]
    precheck --> archived["Replay archived graph to archived head"]
    archived --> validate["Validate expected schema"]
    validate --> reanchor["Re-anchor to active 0001"]
    reanchor --> active
    active --> integrity["Verify active head 0003\nPRAGMA integrity_check plus foreign_key_check"]
    integrity --> ready["Runtime-ready database"]
    ready --> scheduled_backup["Scheduled online backup"]
    scheduled_backup --> encrypt["gzip plus AES-256-GCM"]
    encrypt --> sibling["Sibling restore set and restore drill"]
    ready --> gc["GC planner\ndry-run by default, allowlist-only"]
    gc --> archive_first["Archive-first eligible rows"]
    archive_first --> delete["Guarded deletion after verification"]
    gc --> blocked["facts-depth destructive cleanup blocked\nuntil immutable archive receipt exists"]
    ready --> deletion_eval["Deletion catalog evaluator"]
    deletion_eval --> targets["3 candidates, 25 schema targets\ncode targets Git-restorable"]
    targets --> import_scan["Absolute and relative import reachability scan\nparse/read errors fail closed"]
    import_scan --> eligibility{"Eligible?"}
    eligibility -->|"No"| refuse_delete["Keep code and schema"]
    eligibility -->|"Yes"| governed_delete["Apply cataloged deletion"]
    backup -. "Only recovery source for rows dropped before 0002" .-> sibling
```

## Operational boundaries

- The application is single-user and localhost/Tailscale-scoped; it is not a public multi-tenant service.
- Live scheduler registration, optional provider credentials, and real production population are operational checks, not implied by source-level green tests.
- Rows removed before migration `0002` are recoverable only from a pre-`0002` database backup. Git restores code, not deleted database rows.
- The dormant legacy Gemini fallback compatibility exports are outside the canonical production route and remain one low-priority cleanup item pending an external-import compatibility scan.

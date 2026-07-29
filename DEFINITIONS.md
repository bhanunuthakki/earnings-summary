# Definitions

Canonical terminology for this project. Use these terms verbatim in code (variables, functions, types, columns), comments, commit messages, and PR descriptions. New domain terms must be added here before being used.

## Thought Partner

**Definition.** The program's operating identity — a living system that extracts, explores (Socratically), synthesizes, and learns a user Worldview over time; it treats captures as raw material for thinking, not records to file. Storage is the last step, not the product.
**Lives in.** Cross-cutting identity, realized by the capture → explore → distil → Worldview pipeline (`src/capture/`, the On My Mind feed, `src/synthesis/`, `src/llm/anchors.py`).
**Not to be confused with.** The per-ticker analyst workspace (the HTML report deliverable) — that is the *output* of analysis, not the thinking loop.
**Subsumes.** Informal "assistant" / "chatbot" / "CRUD app" descriptions of the program.

## On My Mind

**Definition.** The reverse-chronological living feed of what the analyst is currently thinking about and reading — each item indexed to themes, holdings, and overall positioning, carrying the action ladder **dismiss · save-for-later · discuss · incorporate-into-research**. The front-of-funnel where the LLM extracts and explores *before* anything is distilled.
**Lives in.** (to be built) the capture feed surfaced in Telegram and the dashboard notecard/library; a read model over `analyst_notes` (`source='capture'`); feeds the Worldview.
**Not to be confused with.** The Worldview (durable, synthesized) — On My Mind is transient working memory that feeds it.
**Subsumes.** The **Wondering** flag and its detection (`wondering_detect`, flag `LEDGER_RESEARCH_TAP`). On My Mind is strictly broader — reading and exploration, not just self-posed questions — and absorbs it.

## Worldview

**Definition.** The durable, evolving model of how the analyst thinks — the synthesized set of Tenets that subtly conditions investment reasoning (hold / add / trim / sell / evaluate).
**Lives in.** (to be built) a durable tenets store; injected into thesis / ask / decision reasoning via the anchor mechanism (`src/llm/anchors.py`).
**Not to be confused with.** A per-ticker thesis (company-specific, in `micro_thesis/holdings/`) — the Worldview is cross-company, about the analyst's *own* reasoning.
**Subsumes.** The merged `influence` analyst-notes kind (PR #701), which is superseded by Tenets.

## Tenet

**Definition.** A single revisable belief-unit in the Worldview — a principle about *how the analyst invests* — with provenance to the insights that formed it; the system proposes revisions the analyst approves and flags contradictions when a new insight conflicts with a standing Tenet.
**Lives in.** (to be built) `insight_notes` with `kind='tenet'`; composes the Worldview.
**Not to be confused with.** A **conviction** (see below) — a `conviction` is a *1–5 confidence rating on a position/decision* (`bucket_for_conviction`, conviction calibration/Brier in `src/advisor/`, and the `conviction` field on `decision_capture`). A Tenet is a cross-company belief about *method*, not a confidence level on a name. Also distinct from a `musing` (an in-the-moment captured thought) and an `insight_note` of `kind='theme'` (a topic cluster, not a belief).
**Subsumes.** — (was proposed as "Conviction" 2026-07-01; renamed to avoid collision with the entrenched `conviction` rating.)

## conviction (rating)

**Definition.** A 1–5 confidence score the analyst assigns to a position/stance/decision, used for calibration (hit-rate by conviction bucket, Brier scoring).
**Lives in.** `src/advisor/context.py`, `src/advisor/memos.py`, the sizing-audit conviction column, `src/research/decision_capture.py` (`conviction` field).
**Not to be confused with.** A **Tenet** (a Worldview belief-unit). Lowercase `conviction` = a rating; a Tenet = a belief.

## Source Taxonomy Component

**Definition.** An immutable, exact source-side XBRL concept, axis, or member identity keyed by its taxonomy namespace, local name, taxonomy name, and taxonomy version, with its source metadata and provenance commitments.
**Lives in.** `source_taxonomy_components` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A **Canonical Metric**; a Source Taxonomy Component describes what a filer or taxonomy asserted, not the economic meaning selected for analysis.
**Subsumes.** Source QName, taxonomy concept, source axis, and source member only when the exact source identity is retained.

## Source Observation Taxonomy Assertion

**Definition.** An immutable, hash-committed assertion of the exact taxonomy name and version used by one preserved reported observation. It cites and verifies the exact extraction run, fact-cell identity seal, reported-anchor payload, observation payload, extraction output, raw entry, and sealed observation set, all of which must exist before the assertion's knowledge clock.
**Lives in.** `source_observation_taxonomy_assertions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A Source Taxonomy Component; the assertion proves which taxonomy governed one observation, while the component identifies one concept, axis, or member inside that taxonomy.

## Canonical Metric

**Definition.** A stable named economic identity whose meaning evolves only through explicit Canonical Metric Definition Revisions, used to compare facts across source taxonomies without making a source QName part of its identity.
**Lives in.** `canonical_metrics`, `canonical_metric_definition_revisions`, `canonical_metric_cells`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A **Source Taxonomy Component** or a fact value; a Canonical Metric defines intended economic meaning, while source facts remain independent assertions.
**Subsumes.** Normalized economic metric and analytical metric.

## Canonical Axis

**Definition.** A stable source-independent dimension axis identity that may be used in Canonical Metric Cells only after explicit source-axis admission.
**Lives in.** `canonical_axes`, `canonical_metric_cell_dimensions`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A source XBRL axis QName, which remains an exact Source Taxonomy Component.
**Subsumes.** Normalized dimension axis and analytical dimension axis.

## Canonical Member

**Definition.** A stable source-independent member identity owned by one Canonical Axis and admitted from exact source members through revisioned mapping evidence.
**Lives in.** `canonical_members`, `canonical_metric_cell_dimensions`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A source XBRL member QName or typed-member value; those remain source evidence and fail closed until admitted.
**Subsumes.** Normalized dimension member and analytical dimension member.

## Source Dimension Mapping Revision

**Definition.** An append-only bitemporal decision mapping one exact source axis or member component to a Canonical Axis or Canonical Member under explicit policy, evidence, and reviewer authority.
**Lives in.** `source_dimension_mapping_revisions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A Metric Mapping Revision, which maps a source concept to a Canonical Metric.
**Subsumes.** Source-axis mapping and source-member mapping.

## Metric Mapping Revision

**Definition.** An append-only, bitemporal decision that records whether one exact Source Taxonomy Component maps to a Canonical Metric and under which policy, method, constraints, evidence, and reviewer authority.
**Lives in.** `metric_mapping_revisions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A Canonical Metric Definition Revision; this is source-to-metric admission evidence, not a change to a metric's meaning.
**Subsumes.** Concept mapping, equivalence decision, and mapping policy result.

## Canonical Metric Cell

**Definition.** The source-independent coordinate for a Canonical Metric at one reporting entity, period, canonical dimension set, unit family, accounting basis, consolidation scope, and optional security scope.
**Lives in.** `canonical_metric_cells` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A v2 Fact Cell; a Canonical Metric Cell intentionally excludes source QName and taxonomy version so multiple retained source assertions can bind to it.
**Subsumes.** Canonical fact coordinate and normalized metric cell.

## Fact-Cell Canonical Binding Revision

**Definition.** An append-only bitemporal interpretation of one preserved source observation within a v2 Fact Cell. A reported observation may bind through its exact taxonomy assertion, Metric Mapping Revision, and Source Taxonomy Component to one Canonical Metric Cell; a derived observation without an explicit canonical basis is instead terminally quarantined with a committed reason.
**Lives in.** `fact_cell_canonical_binding_revisions` and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A one-binding-per-cell shortcut, source fact mutation, or deduplication; each observation owns independent revision history and bindings never merge or erase source assertions.
**Subsumes.** Fact-to-metric binding and canonicalization link.

## Ontology Snapshot Seal

**Definition.** A hash-committed membership set of the ontology records known by an explicit cutoff, used to admit later research snapshots reproducibly.
**Lives in.** `ontology_snapshot_seals`, `ontology_snapshot_members`, and `src/provenance/metric_ontology.py`.
**Not to be confused with.** A wall-clock database backup; it is an explicit bitemporal governance cutoff with verified members.
**Subsumes.** Ontology seal and mapping snapshot.

## Canonical Fact Candidate Universe

**Definition.** The exhaustive, ordered, hash-committed set of admitted source observations bound to one Canonical Metric Cell as known at an explicit cutoff. Its membership is derived only from sealed admission and ontology records; callers cannot supply, omit, or reorder candidates.
**Lives in.** `canonical_fact_candidate_universe_revisions`, `canonical_fact_candidate_dispositions`, and `src/provenance/canonical_fact_resolution.py`.
**Not to be confused with.** A source-fact candidate set; this set spans every source QName bound to the one canonical coordinate.
**Bound.** Relation construction is intentionally capped at 500 admitted observations per Canonical Metric Cell and cutoff; exceeding it fails closed and requires an explicit partitioning policy rather than an unbounded pairwise graph.

## Canonical Fact Relation Set

**Definition.** An immutable, revisioned assertion graph over a Canonical Fact Candidate Universe. It preserves duplicate-entry ordinals and records equivalence, conflict, amendment, recast, and supersession evidence without collapsing source assertions.
**Lives in.** `canonical_fact_relation_set_revisions`, `canonical_fact_relation_assertions`, and `src/provenance/canonical_fact_resolution.py`.
**Not to be confused with.** A value-selection rule; relations are evidence, while a Canonical Fact Resolution applies a named deterministic policy to that evidence.

## Canonical Fact Resolution

**Definition.** An append-only, bitemporal selection or explicit unresolved/retired outcome for one Canonical Metric Cell. A resolution references the exact sealed candidate universe and relation set used, and never averages conflicting source assertions.
**Lives in.** `canonical_fact_resolution_revisions`, `canonical_fact_resolution_snapshot_seals`, and `src/provenance/canonical_fact_resolution.py`.
**Not to be confused with.** A source-cell resolution; it is the cross-QName decision at a canonical coordinate.

## Document Processing Obligation

**Definition.** A revisioned, bitemporal requirement that one exact recorded document complete one named processing lane under a committed policy, derived exhaustively from source obligation, document, and immutable evidence state as known at an explicit cutoff.
**Lives in.** `document_processing_obligation_revisions`, `src/provenance/research_snapshot.py`, and `alembic/versions/0245_document_processing_research_snapshots.py`.
**Not to be confused with.** A source acquisition duty; a Document Processing Obligation begins from a recorded document and governs the transformations required before research use.
**Subsumes.** Required parser lane, extraction requirement, and document-processing duty.

## Document Processing Disposition

**Definition.** The final sealed outcome for one Document Processing Obligation, with exactly one terminal status and an exhaustive ordered set of committed evidence members that prove success or a valid terminal exception.
**Lives in.** `document_processing_disposition_headers`, `document_processing_disposition_members`, `document_processing_disposition_seals`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** A mutable job status or retry log; it is an immutable final decision whose members and seal are verified symmetrically against live evidence.
**Subsumes.** Processing result, lane disposition, and extraction outcome.
**Current admission boundary.** `filing_xbrl` can succeed only through the exact native 0242 extraction-disposition seal and its verified Source Fact Publication. Every other applicable lane fails closed until a native closure adapter can recompute its ordered outputs and final seal; synthetic or caller-attested commitments never admit.

## Document Processing Snapshot

**Definition.** An exhaustive, ordered, hash-sealed set containing every applicable Document Processing Obligation and its one verified Document Processing Disposition as known at one explicit cutoff.
**Lives in.** `document_processing_snapshot_headers`, `document_processing_snapshot_members`, `document_processing_snapshot_seals`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** A current processing dashboard or a caller-supplied document list; membership is derived internally and cannot change when later evidence arrives.
**Subsumes.** Processing completeness seal and document readiness snapshot.

## Native Processing Evidence Seal

**Definition.** An immutable header/member/final-seal publication derived from one exact successful extraction run and the complete ordered native rows required for a supported Document Processing lane. Its public verifier re-derives membership from the pinned native run and compares every locator, content commitment, clock, and final member-set digest.
**Lives in.** `document_processing_evidence_headers`, `document_processing_evidence_members`, `document_processing_evidence_seals`, `src/provenance/document_processing_evidence.py`, and `alembic/versions/0248_native_processing_closure_adapters.py`.
**Not to be confused with.** A successful extraction attempt or caller-supplied evidence list; neither proves that the native output set is complete.
**Current admission boundary.** HTML hierarchy, PDF native text/OCR/tables, standalone image OCR, PPTX slides/charts/tables, XLSX workbook/sheets/named tables, transcript turns, and fully time-coded transcript speakers can seal. Each applicable lane still fails closed when its exact native inventory is missing, quarantined, old, or tampered.

## PDF Table Extraction Artifact

**Definition.** An append-only header/member/final-seal publication over one exact PDF byte stream and one pinned PyMuPDF/MuPDF dual-detector configuration. Its ordered members preserve every page disposition and detected table, row, cell, coordinate, nested commitment, and explicit no-table proof.
**Lives in.** `pdf_table_extraction_artifact_headers`, `pdf_table_extraction_artifact_members`, `pdf_table_extraction_artifact_seals`, `src/provenance/pdf_table_extraction.py`, and `src/provenance/document_processing_evidence.py`.
**Not to be confused with.** A claim of semantic table exhaustiveness. The artifact proves exact detector-relative coverage; encrypted, scanned, image-only, malformed, ambiguous, or resource-capped inputs remain quarantined and cannot satisfy the `pdf_table` lane.

## Research Snapshot

**Definition.** An immutable, ordered, hash-sealed research evidence boundary that binds exact Document Processing Snapshots and every requested verified corpus, search, fact-publication, ontology, canonical-resolution, canonical-fact-projection, and embedding-promotion seal at one cutoff.
**Lives in.** `research_snapshot_headers`, `research_snapshot_members`, `research_snapshot_seals`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** A database backup, current-view query, or generated research report; it is the reproducible admission boundary those consumers must cite.
**Subsumes.** Research evidence snapshot and research-readiness seal.

## Research Snapshot Admission

**Definition.** A typed, fail-closed verification result proving that a Research Snapshot contains exactly one valid terminal member for every requested lane and that every referenced commitment and clock still matches immutable live evidence.
**Lives in.** `ResearchSnapshotAdmission`, `admit`, and `src/provenance/research_snapshot.py`.
**Not to be confused with.** Snapshot creation or optimistic readiness; admission is granted only after symmetric verification of sealed membership and public-verifier results.
**Subsumes.** Research readiness decision and snapshot admission result.

## Source Fact Publication Stream

**Definition.** Database-assigned, monotonically ordered events over verified sealed Source Fact Publications. Stream order is strict but gaps are allowed; consumers use it as the replay high-watermark.
**Lives in.** `source_fact_publication_stream`, `source_fact_stream_clock`, `src/provenance/source_fact_stream.py`, and `alembic/versions/0246_source_fact_publication_stream.py`.
**Not to be confused with.** Knowledge or recorded clocks, or a publication member ordinal; the stream orders sealed publication events for consumption and replay.

## Canonical Fact Projection Generation

**Definition.** An immutable checkpoint or delta read generation derived from one exact Canonical Fact Resolution Snapshot and Ontology Snapshot at a verified Source Fact Publication Stream watermark. It commits bounded batches, deterministic digest buckets, explicit upserts and tombstones, and the effective fact count.
**Lives in.** `canonical_fact_projection_generations`, `canonical_fact_projection_entries`, `canonical_fact_projection_batches`, `canonical_fact_projection_buckets`, `canonical_fact_projection_seals`, and `src/search/canonical_fact_projection.py`.
**Not to be confused with.** The legacy whole-plane Structured Fact Search Projection; a Canonical Fact Projection contains only selected canonical metric coordinates and cannot be admitted without its strict audit receipt.

## Heterogeneous Retrieval Trace

**Definition.** An immutable, replay-verifiable trace over one exact Research Snapshot that records the closed candidate universe, deterministic ranker inputs, ordered `FactHit | DocumentHit` results, and every returned hit's source commitment.
**Lives in.** `heterogeneous_retrieval_trace_headers`, `heterogeneous_retrieval_trace_candidates`, `heterogeneous_retrieval_trace_results`, `heterogeneous_retrieval_trace_seals`, and `src/search/heterogeneous_retrieval.py`.
**Not to be confused with.** An answer citation list or an unsealed vector-search response; the trace proves what retrieval considered and returned before synthesis.

## Embedding Runtime Artifact

**Definition.** A canonical, path-free, content-addressed manifest of every local file, package/runtime version, execution provider, and explicit setting that can affect one embedding model coordinate. Its digest is shared by evaluation, promotion, successful vector artifacts, vector projection seals, and exact semantic receipts; the local bytes and component versions are verified before and after offline model initialization.
**Lives in.** `src/search/embedding_runtime_artifact.py`, `search_embedding_model_promotions.runtime_artifact_json`, `search_embedding_model_promotions.runtime_artifact_sha256`, `search_embedding_artifacts.runtime_artifact_sha256`, `search_projection_seals.runtime_artifact_sha256`, and `alembic/versions/0249_embedding_runtime_artifact_binding.py`.
**Not to be confused with.** A model name, cache path, download URL, or digest inferred after a build. Historical unbound rows remain historical and cannot be promoted into investor-grade exact semantic retrieval.

## Exact Semantic Retrieval Receipt

**Definition.** A locally recomputable commitment to the complete ordered top-k from a bounded full scan of one sealed canonical float32 vector projection. It binds the query and query vector, current embedding promotion and evaluation, exact Embedding Runtime Artifact, model and dimensions, projection/config/storage seals, artifact set, scan cap, scores, and ordering.
**Lives in.** `src/search/exact_semantic.py` and the semantic-receipt section of `src/search/heterogeneous_retrieval.py`.
**Not to be confused with.** An opaque vector-service response or approximate-nearest-neighbor receipt. Those cannot enter an investor-grade Heterogeneous Retrieval Trace unless a separately governed evaluation and admission contract is added.

## Shadow Read Contract

**Definition.** A database-neutral, canonical-JSON protocol for asynchronously reproducing sealed projections and retrieval traces in a secondary read store. It binds the exact stream cursor, cutoff, ontology, resolution, Research Snapshot, and projection seals; hash-chained batches are idempotent under duplicate or reordered delivery and checkpoints advance by compare-and-swap.
**Lives in.** `src/provenance/postgres_shadow.py`.
**Not to be confused with.** A deployed PostgreSQL replica or write cutover. The contract defines parity and replay rules; an operational shadow still requires adapters, deployment, restore tests, lag monitoring, and owner-approved read promotion.

## Legacy Canonical Parity Report

**Definition.** A cutoff-pinned, read-only, keyset-paged comparison that follows the exact accepted legacy-evidence bridge into one ontology-bound Canonical Metric Cell and its sealed, audited Canonical Fact Projection entry. It records one explicit terminal disposition for every legacy row, exact field differences, duplicate-coordinate cardinality, truncation, and cutover readiness.
**Lives in.** `src/provenance/legacy_canonical_parity.py`.
**Not to be confused with.** Row-count equality, label/QName matching, or a live reader cutover. `cutover_ready` requires a complete untruncated scan with no mismatches or blocking legacy-side dispositions; canonical-native-only coordinates are reported separately.

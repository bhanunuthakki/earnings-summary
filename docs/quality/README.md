# Code-quality measurements

This directory starts the deterministic measurement layer for the 9+ code-
quality program. The artifacts in this first slice are measurements, not score
admission, current-tree ratchets, or a claim that the repository has reached a
9+ grade.

The two initial receipts scope the pre-program commit
`09d35d1a2785ff7e6a218031eb43952781be3a93`. Their source and scanner hashes
make both source drift and measurement-definition drift explicit.

Regenerate them from the repository root:

```bash
python execution/capture_architecture_measurement.py \
  --revision 09d35d1a2785ff7e6a218031eb43952781be3a93 \
  --output docs/quality/architecture-initial-09d35d1a.json
python execution/analyze_code_duplicates.py \
  --revision 09d35d1a2785ff7e6a218031eb43952781be3a93 \
  --out docs/quality/duplicates-initial-09d35d1a.json
```

Later train slices own independently verified score evidence and enforcement.
These raw receipts cannot award score points on their own.

## Operational reachability

Generate the tracked-source operational graph and validate the checked-in
reviewed dispositions:

```bash
python execution/build_operational_reachability.py \
  --output .tmp/quality/reachability-check.json
```

The collector excludes `docs/quality/` evidence artifacts from the operational
population and records those exclusions explicitly. The three disposition
manifests classify production-reachable dynamic imports, reflective attribute
accesses, and process launches. Each decision is bound to the exact parser,
source and input-manifest hashes plus a fingerprint of the reviewed source
line. Missing, malformed, stale, duplicated, or forged evidence leaves the raw
edge unknown and closure at `HOLD`.

At this slice's source state, collection is `COMPLETE`, all 137 production
unknown edges have reviewed dispositions, and production closure is `PASS`.
The remaining 91 unknown edges comprise 90 test or instruction-test edges and
one non-production-reachable source edge in `src/search/fact_projection.py`.
They stay visible and cannot be traversed as reachability proof. A successful
CLI exit still proves collection completion only. Reachability closure does not
admit a code-quality score or authorize deletion; later lifecycle evidence owns
those decisions.

## Raw performance timing

Capture raw local timing without admitting performance or score:

```bash
python execution/capture_performance_baseline.py \
  --command "python -c \"print('ok')\"" \
  --output .tmp/quality/performance-baseline.json \
  --samples 7
```

The collector runs one unscored warmup followed by 1-21 measured repeats;
fewer than seven are explicitly marked insufficient for stability. It
labels samples ordinally (`measured` with a 1-based ordinal, never
cold/warm), and reports median, MAD, a seeded deterministic bootstrap 95% CI,
and stability. Every receipt records the requested command, resolved argv,
HEAD revision, tracked Python source hash, declared config hash,
scanner/module hash and version, runtime identity, elapsed samples, exit
codes, exact output hash/size, a bounded redacted preview, provenance, and
collection status. The source identity says whether the complete repository
working tree is clean at `HEAD` or differs from the recorded revision.

Commands are caller-trusted and run without a shell. Credential-like
environment variables are removed, but the collector does not claim network
isolation, and callers must keep benchmark output bounded because subprocess
capture is in memory. `COMPLETE` means only that raw collection finished; it
does not make dirty-tree or revision-scoped evidence admissible. Successful
collection is always admission `HOLD` because causal and paired performance
evidence is deferred; receipts belong under ignored `.tmp/` paths.

## Lifecycle/reconciliation reproduction

Lifecycle inventory and roadmap reconciliation are fail-closed producers.
Reachability is a prerequisite input, not a silent side effect:

```bash
python execution/build_operational_reachability.py \
  --output .tmp/quality/reachability-check.json
python execution/classify_operational_lifecycle.py \
  --output .tmp/quality/lifecycle-inventory.json
python execution/reconcile_quality_baseline.py \
  --output .tmp/quality/roadmap-reconciliation.json
```

The lifecycle CLI never regenerates the graph. A missing, malformed, or
stale `.tmp/quality/reachability-check.json` is an expected operational
error (`LifecycleError`, exit 1). A dirty worktree is never `PASS`.

## Exact-subject evidence bundle (BHA-147)

Phase A collects hash-bound raw producer bytes in ignored staging without
claiming bundle identity. Phase B byte-preserves allowlisted
`docs/quality/*.json` sources and mints deterministic
`quality-score-admission/v1` receipts. Scoring stays owned by
`src/quality/scoring.py`; this layer never scores.

```bash
python execution/collect_evidence_bundle.py --mode collect \
  --repo-root . --staging-dir .tmp/quality/evidence-bundle
python execution/collect_evidence_bundle.py --mode assemble \
  --repo-root . --staging-dir .tmp/quality/evidence-bundle \
  --output-dir docs/quality
# after committing only allowed JSON evidence:
python execution/collect_evidence_bundle.py --mode validate \
  --repo-root . --subject <40-hex> --bundle <40-hex>
python execution/collect_evidence_bundle.py --mode record \
  --repo-root . --bundle <40-hex> --output .tmp/quality/score-evidence.json
```

Collector properties:

- Brackets exact 40-hex `HEAD`/`HEAD^{tree}` and full
  `status --porcelain --untracked-files=all` cleanliness before and after.
  Any dirty tree, HEAD/tree race, or unavailable identity is typed `HOLD`
  with bounded violations.
- Records per artifact the subject commit/tree, generator path/hash/version
  or exact argv (no shell), native scope label (e.g. `WORKTREE` preserved),
  embedded subject, schema, exact raw-bytes SHA-256, and
  collection status. Raw Pyright/timing bytes are preserved verbatim.
- Staging must live under ignored `.tmp/` and never embeds a bundle commit.
  Manifest integrity (`manifest_hash`) is computed over the manifest with
  `manifest_hash` excluded, so it is not self-referential.
- Tests inject a `runner(argv, repo_root)`; real use invokes existing local
  CLIs without a shell and never touches production DB or network.
- Default collection has eight typed producers including test-db and
  lifecycle. Accepted exit codes are declared and recorded; exit 2 may
  mean successfully captured semantic HOLD. Acquisition completeness,
  typed bundle validity, per-slot admission, and score outcome stay
  distinct: typed-valid HOLD bytes are preserved verbatim, while
  typed-invalid bytes or acquisition failure are typed HOLD.

Assembler properties:

- Consumes only the verified Phase A manifest plus staged bytes, rechecks
  SHA-256 stability, byte-preserves sources into the explicit canonical
  allowlist, and mints receipts for exactly the 14 non-architecture
  `SCORE_BLOCKS` and 10 `HARD_GATES`. Each receipt binds the subject, the
  registered generator `src/quality/admission_policy.py` at its exact
  subject hash, and only policy-required available typed sources excluding
  itself (empty only for fail-closed unadmitted slots).
- Typed-valid `HOLD` sources still assemble `COMPLETE` but yield fail
  admissions; honest source `HOLD` never becomes admission `PASS`.
  Insufficient proof for a score slot yields a fail admission whose cited
  source tuple may be empty; that does not by itself make an otherwise
  complete typed collection bundle `HOLD`. Assembly `HOLD`s for a
  declared collection source that failed or is absent from its
  manifest/staging contract, typed-invalid source bytes, or unknown
  registry/path/schema material. Only three narrow rules can currently pass;
  the other 21 stay deliberately unadmitted until dedicated evidence
  exists. This honest fail-closed phase is not a 9+ claim.
- Writes are atomic and require a repo-contained canonical relative path
  that is a non-symlink regular single-link file with exact binding;
  direct, symlink, hard-link, or escape aliases are rejected.
  `subject..bundle` diff must contain only allowed JSON, subject must be
  ancestor of bundle, bundle must be ancestor of
  `refs/remotes/origin/main`, and no bundled JSON may embed the bundle
  SHA. Bundle identity is recorded only after commit in an ignored external
  `ScoreEvidence` manifest; a discarded pre-squash SHA cannot verify.

### Staged exact-subject reconciliation

```bash
python execution/reconcile_quality_baseline.py \
  --subject-root <subject-dir> --staged-manifest <manifest.json> \
  --output .tmp/quality/roadmap-reconciliation-staged.json
```

Staged mode uses hash-bound explicit receipt inputs via `--subject-root` +
`--staged-manifest` (both required, mutually exclusive with `--repo-root`); it never regenerates the graph. Any mismatch/tamper/stale/mixed subject HOLD applies: forged, stale, or mixed-subject receipts stay `HOLD`, and an output aliasing a protected input or the manifest is rejected.

### Direct-builder invocation dispositions

```bash
python execution/audit_test_db_patterns.py \
  --root . --dispositions .tmp/quality/invocation-conversions.json \
 --output .tmp/quality/test-db.json
```

RETAIN default holds without `--dispositions`. CONVERT only with exact
source locator/hash plus an offline strict parity receipt holding
`schema_version` `test-db-parity/v1`, literal `PASS`, `invocation_id`,
`path`, `locator` (`start_line/start_col/end_line/end_col`), and matching
`source_sha256`; the conversion record separately holds the
`parity_receipt` path, canonical `owner_issue`/`reason`, and
timezone-aware future `expires_at`. Unresolved/dynamic HOLD applies to
unresolved or dynamic invocations.

### Durable disposition

no surface change — preserves the existing quality-measurement/score-evidence contract; no Operations registry field, scheduler task, service, operator action, or runtime panel is added.

collection/assembly/validation/record are manual local-only non-production operations. `.tmp` is ignored external staging with no network or production DB. post-commit validation/ScoreEvidence recording are caller-owned, and failures remain HOLD/errors.

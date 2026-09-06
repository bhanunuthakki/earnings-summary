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
The remaining 88 unknown edges comprise 87 test or instruction-test edges and
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

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

## Raw operational reachability

Generate the tracked-source operational graph without admitting reviewed
dispositions or awarding closure:

```bash
python execution/build_operational_reachability.py \
  --output .tmp/quality/reachability-check.json
```

The collector excludes `docs/quality/` evidence artifacts from the operational
population, records those exclusions explicitly, and always reports closure as
`HOLD` until a later train slice reviews the raw unknown edges. A successful
exit proves only that collection completed; it is not a reachability-closure or
score claim.

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

# 9+ deterministic code-quality score contract

This history record describes the executable score frozen for the 9+ cleanup
program. It does not authorize product behavior changes, production writes,
schema removal, scheduler changes, or deletion. The current program and its
evidence checklist are owned by the Linear project `Earnings Summary — 9+ Code
Quality` and issue `BHA-103`; the implementation begins with `BHA-112`.

## Frozen denominator and architecture definitions

- An executable module is a git-tracked `.py` file under `src/` or `execution/`.
- Physical LOC is Python `splitlines()` count. Nonblank LOC contains a
  non-whitespace character. Noncomment LOC is a nonblank physical line that
  does not contain a Python COMMENT token; an inline-comment line remains code.
- An internal edge is a normal or relative AST `Import`/`ImportFrom` target that
  resolves to the executable-module set. Dynamic and operational edges belong
  to the separate reachability oracle and cannot be inferred away here.
- An SCC is a Tarjan strongly connected component containing at least two
  modules. Fan-out is the number of unique resolved internal module targets.
- Frozen composition roots are `execution/comments_server.py` and
  `src/pipeline/portfolio_panel.py`. The former must be at most 600 noncomment
  LOC and the latter at most 200; all internal module fan-out must be at most 25.
- A facade is a `*_facade.py` module or a frozen composition-root facade. It may
  re-export, register, and compose, but may not perform database, network,
  filesystem, or subprocess work. Every public function must be fully typed.
- Module-shape full credit requires at most 35 modules above 1,000 noncomment
  LOC and no more than three modules at or above 3,000 LOC. Each declarative or
  generated exception also needs owner, evidence, removal issue, expiry within
  90 days, and a blocking cap; an exception never raises these numeric caps.

The architecture ratchet rejects growth in total executable noncomment LOC,
large-module counts, SCC count/membership/maximum size, maximum fan-out, or
facade violations. This prevents scoring through renames, file moves, or
splitting a large implementation into equally coupled fragments.

## Score semantics

`execution/score_code_quality.py` owns the immutable 17 blocks totaling exactly
100 points. A block receives all its points only on `pass`; it receives zero on
`fail`. Missing evidence yields `HOLD`, never partial or discretionary credit.
The score is displayed as points divided by ten to one decimal place without
upward rounding. A result can be `PASS` only at 90 points or higher with every
hard gate present and passing, no architecture regression, and evidence scoped
to the same commit as the architecture receipt.

The ten hard gates, block thresholds, companion benchmarks, exception rules,
and anti-gaming rules are frozen verbatim in the evidence-backed roadmap linked
from BHA-103. Judges may return PASS, REVISE, BLOCK, or HOLD, but cannot alter
the deterministic score.

## Reproducible commands

Create the baseline architecture receipt at the originally graded commit:

```console
.venv/bin/python execution/score_code_quality.py \
  --revision 09d35d1a2785ff7e6a218031eb43952781be3a93 \
  --architecture-only \
  --output .tmp/quality/architecture-baseline.json
```

Score an exact commit against that baseline and a validated evidence manifest:

```console
.venv/bin/python execution/score_code_quality.py \
  --revision HEAD \
  --baseline .tmp/quality/architecture-baseline.json \
  --evidence .tmp/quality/score-evidence.json \
  --output .tmp/quality/score-result.json
```

Large receipts stay under `.tmp/`. A non-PASS score exits nonzero so the command
can be a blocking ratchet without parsing prose.

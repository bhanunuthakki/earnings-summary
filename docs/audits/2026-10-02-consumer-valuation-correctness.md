# Valuation reader correctness

Date: October 2, 2026. Scope: the first consumer repair in the retail research
roadmap. This is source work, not a model refresh or production installation.

## Problem and change

An earnings brief could select a newer superseded valuation and call its saved
price live. A supporting allocation factor could rank a stored valuation without
checking its financial-input readiness.

Earnings context now uses the shared current, top-level valuation reader. It
labels the stored estimate, quote observation time, valuation date and financial
period separately. Unaccepted models retain their limitations and do not produce
a value/quote signal. Selection and readiness share one database read snapshot.
The caller's transaction and row factory remain intact.

Historical previews exclude a current model calculated after their cutoff.
Quote clocks are checked independently even when malformed model evidence makes
readiness exit early. Missing, ambiguous and after-cutoff quote times cannot
supply a quote amount. This does not implement historical valuation selection.

The supporting return factor requires shared readiness for the exact selected
run and ticker under one read-only snapshot. Rejection reasons reach the existing
notes and hidden-factor state. Sanity exclusion remains. The retired standalone
allocation interface is not restored. No readiness reason was weakened.

## Evidence and limits

The original regression reproduced a superseded estimate of 999 winning over
the current estimate of 120. A separate regression reproduced an unaccepted
scalar valuation providing a positive return factor.

Independent GPT-6.1 Sol review initially blocked the candidate. It found that
malformed model evidence could bypass the quote cutoff check. Two regression
cases reproduced that defect before repair. It also requested explicit dates in
two fixed-date tests. Both findings were repaired. Its bounded delta review gives
an advisory PASS. This is not a calibrated release or valuation verdict.

The repaired targeted set passes 73 tests. Accepted-path arithmetic tests use an
explicit controlled readiness seam; they do not certify synthetic inputs. Real
readiness rejection, clock limits, transaction preservation, missing databases,
and concurrent persistence have separate negative or integration coverage.
The complete project gate is recorded in the delivery receipt.

Three reachability aggregate hashes are rebound to the changed source bytes.
All 173 classifications, targets, locators and fingerprints are preserved. The
existing exact test-owned unknown-edge oracle is unchanged. Removing two inline
test suppressions lowers the tests ceiling from 1057 to 1055; no ceiling rises.

## Operations disposition

Tested **no surface change**: the candidate adds no route, operation, job,
scheduler, provider or database writer. The changed production paths are
read-only consumers; existing missing-factor diagnostics carry the rejection.
The targeted transaction and read-only tests support this boundary. No schema
change, production data write or external collection was performed.

Current valuations remain subject to their separate input and scenario
acceptance. Native Windows qualification, deployed performance and the remaining
research roadmap are separate work. Unrelated concurrent UI and earnings work
is preserved outside this candidate.

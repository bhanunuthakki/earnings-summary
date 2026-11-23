# Custom full-suite merge queue

Status: **active since 2026-11-23.** Ruleset 20593590 ("Require Queue Gate on main")
requires `Queue Gate` from App 5881047 and blocks force pushes and deletion, with no
bypass actors. Pull requests run selected tests and check types only in changed
files; the queue runs the complete suite and the population ceilings on every
candidate before it reaches `main`.

## Why a custom queue

GitHub's native merge queue is available only to organization-owned repositories.
This repository stays a public personal repository with $0 runner spend. A
`GITHUB_TOKEN`-only design cannot prove where a status came from: any pull-request
workflow can create a job named `CI Gate` under the same GitHub Actions app. A
private GitHub App gives the queue's status a source that a ruleset can pin.

Research and primary sources: `.tmp/ci-audit/custom-queue-research/README.md`
(local only, not tracked).

## How it works

1. The owner adds the `merge-queue` label to an open, non-draft pull request whose
   head branch is in this repository. The label approves that exact head.
2. `merge-queue.yml` runs from trusted `main`, one controller at a time. It selects
   the oldest labelled entry. It waits for GitHub's test merge commit **C** whose
   parents are exactly [`main` **B**, head **H**].
3. Main's `ci.yml` runs the complete suite on C. Every path group is forced on.
   The called jobs get a read-only contents token, no secrets, no persisted
   checkout credentials and `cache-mode: read`.
4. `finalize` runs on a fresh runner from trusted `main`, in the protected
   `merge-queue` environment. It requires:
   - the full-suite result is `success`;
   - every one of the 23 expected jobs ran exactly once and succeeded;
   - B, H and the label are unchanged, and C's parents are still [B, H].

   Then the App posts `Queue Gate: success` on C. The workflow pushes C to `main`
   with `--force-with-lease=refs/heads/main:B`, so the push fails if `main`
   moved. GitHub marks the pull request merged because H is now in `main`.
5. A failed, cancelled or incomplete candidate is removed from the queue with a
   comment. It is not rerun. Re-apply the label after a fix.
6. `merge-queue-revoke.yml` removes the label when a new head is pushed, the pull
   request is reopened, or it returns to draft.

`main` gets GitHub's merge commits ("Merge H into B"), which matches the current
merge-commit history.

## Cost

Standard GitHub-hosted runners on a public repository: $0. No server is needed.
Each queued pull request runs the complete suite once more (about 5–6 minutes of
wall time on free runners). The push to `main` also starts the existing `main` CI run.

## Owner setup (done 2026-11-23)

1. **Register a private GitHub App** under the personal account.
   - Webhook: off.
   - Repository permissions: Contents **read and write**, Commit statuses **read
     and write**, Workflows **read and write**. Metadata read is automatic. Grant
     nothing else.
   - Where it can be installed: only this account.
2. **Install the App** on `bhanunuthakki/earnings-summary` only.
3. **Create the environment** `merge-queue`. Set its deployment branch policy to
   the `main` branch only.
4. In that environment, add:
   - secret `MERGE_QUEUE_PRIVATE_KEY`: the App's private key (`.pem` contents);
   - variable `MERGE_QUEUE_CLIENT_ID`: the App's Client ID (not a secret).
5. **Create the `merge-queue` label.**
6. **Approve the ruleset change** only after the acceptance checks pass:
   - in ruleset 20593590, replace the required `CI Gate` check with
     `Queue Gate`, and pin its source to the new App (`integration_id` = App ID);
   - add *Block force pushes* (non-fast-forward) and *Restrict deletions* with no
     bypass actors.

   Do not add the App as a bypass actor on the required-check rule. The App
   satisfies the rule by posting the status; it does not skip it.

The App needs Workflows write because C can contain `.github/workflows` changes.
Without that permission, GitHub rejects those pushes.

## Acceptance

The queue code and its environment admit only `main`, so a temporary test branch
was not possible. The non-merging checks ran on `main` under the old `CI Gate`
rule; the rest ran after the owner approved activation.

- [x] A direct update of `main` without `Queue Gate` is rejected. 2026-11-23: an
  API ref update to empty commit `34875dd` failed with HTTP 422 "Required status
  check "Queue Gate" is expected"; `main` stayed `ca05573`.
- [ ] A pull-request job named `Queue Gate` (Actions app) does not satisfy the pinned rule.
- [x] A failing candidate is dequeued once, gets no status, and the branch is
  unchanged. Probe #1794, run 45115381135, candidate `28ef665`: one comment, label
  removed, no statuses on the candidate, `main` stayed `ca05573`.
- [x] A passing candidate moves the branch to exactly C; C's parents are [B, H].
  #1793, run 45122501460: `main` moved to `59725f5` with parents
  [`ca05573`, `1c84dd7`] and one `Queue Gate: success` status from the App.
- [ ] Moving the branch during the run makes the lease reject the push.
- [ ] Pushing a new head during the run removes the label, and finalization refuses.
- [ ] A cancelled shard and a missing job each fail closed (unit tests in
  `tests/test_merge_queue.py` only; not yet observed live).
- [x] No personal access token is used anywhere. The only secret any workflow reads
  is `MERGE_QUEUE_PRIVATE_KEY`; other steps use `github.token` or the App token.
- [x] Candidate jobs cannot save a cache in the `main` scope. During probe #1794 the
  only new `main` cache was `pyright-receipt-v1-ca05573…`, which main's own push run
  saves; the candidate saved none.
- [ ] Several labelled pull requests are all processed, in order, with no lost entry.
- [x] A pull request that changes `.github/workflows` merges through the queue (#1793).
- [x] Read back the live ruleset: `GET /repos/.../rules/branches/main` returns
  `required_status_checks` [`Queue Gate`, integration 5881047], `non_fast_forward`
  and `deletion`, all from ruleset 20593590.

## Known limits

- Serial only: one candidate at a time. There is no batching and no native UI.
- Anyone who can edit `main`'s workflows or the repository settings is inside the
  trust boundary.
- Workflow `cache-mode` is newer than actionlint 1.7.12. Local actionlint runs
  ignore only that key. GitHub has not validated the new workflows yet, because
  they first run from `main`.

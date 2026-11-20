# Custom full-suite merge queue

Status: **prepared, not active.** No App, secret, environment or ruleset exists yet.
Until the owner completes the setup and the acceptance checks below pass, `main` is
protected only by the existing `CI Gate` ruleset. Do not narrow pull-request tests
before then.

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

## Owner setup (not done; each step needs the owner)

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

## Acceptance before claiming enforcement

Run these against a temporary protected test branch and ruleset first, then once
on `main` after the owner approves activation. Record run IDs and SHAs here.

- [ ] A direct push to the protected branch without `Queue Gate` is rejected.
- [ ] A pull-request job named `Queue Gate` (Actions app) does not satisfy the pinned rule.
- [ ] A failing candidate is dequeued once, gets no status, and the branch is unchanged.
- [ ] A passing candidate moves the branch to exactly C; C's parents are [B, H].
- [ ] Moving the branch during the run makes the lease reject the push.
- [ ] Pushing a new head during the run removes the label, and finalization refuses.
- [ ] A cancelled shard and a missing job each fail closed.
- [ ] No personal access token is used anywhere.
- [ ] Candidate jobs cannot save a cache in the `main` scope.
- [ ] Several labelled pull requests are all processed, in order, with no lost entry.
- [ ] A pull request that changes `.github/workflows` merges through the queue.
- [ ] Read back the live ruleset and attach it here.

## Known limits

- Serial only: one candidate at a time. There is no batching and no native UI.
- Anyone who can edit `main`'s workflows or the repository settings is inside the
  trust boundary.
- Workflow `cache-mode` is newer than actionlint 1.7.12. Local actionlint runs
  ignore only that key. GitHub has not validated the new workflows yet, because
  they first run from `main`.

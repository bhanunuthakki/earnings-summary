# CI audit — 2026-11-19

Baseline: last 150 workflow runs across both workflows, newest first, retrieved before changes. Window: 2026-11-09T16:28:14Z to 2026-11-19T18:24:01Z.

## Method

- Source: `gh api repos/bhanunuthakki/earnings-summary/actions/runs?per_page=100&page={1,2}`; first 150 runs. Jobs: `actions/runs/{id}/jobs?per_page=100&filter=all`. Every response checked for pagination truncation.
- 61 CI runs and 89 Public Boundary runs; 1,495 job records. Two runs have second attempts. 42 copied-forward job records have `started_at < created_at`; these are retained as anomalies, excluded from execution/queue/cost statistics to avoid counting prior work twice. 155 skipped/unassigned records have no executed runner. 1,298 executed jobs remain: 1,253 success, 42 failure, 3 cancelled.
- Waiting is job creation to runner start. Dependency wait is workflow creation to job creation, reported separately. It is not runner queuing.
- Setup is runner start to first checking step. Running includes all remaining work, teardown, and runner gaps. Setup steps are checkout, runtime setup, dependency/cache restore/install, SQLite build/preload, interpreter resolution, and environment activation. Setup includes browser installation; npm install embedded in the React check remains in running.
- Percentiles use nearest rank, seconds. Cancelled/failed jobs remain in job statistics. PR turnaround uses non-cancelled completed PR runs, including failures, from creation to last update. A quick failure does not prove a passing pipeline is fast. Separate successful-PR statistics below.

## Every job

Each cell is p50/p95 seconds. Job counts include all executed attempts, except the copied-forward records above.

| Workflow / job | n | Waiting | Dependency wait | Setup | Running |
|---|---:|---:|---:|---:|---:|
| CI / CI Gate | 63 | 2.0/4.0 | 390.0/656.0 | 6.0/11.0 | 2.0/4.0 |
| CI / Check Changed Paths | 61 | 2.0/3.0 | 1.0/2.0 | 8.0/14.0 | 2.0/3.0 |
| CI / Design Sync | 58 | 2.0/104.0 | 14.0/44.0 | 54.0/633.0 | 266.0/313.0 |
| CI / Fast Signal | 50 | 2.0/5.0 | 14.0/39.0 | 18.0/47.0 | 10.0/19.0 |
| CI / Native Windows Application Tests | 50 | 2.0/25.0 | 14.0/39.0 | 79.0/89.0 | 169.0/247.0 |
| CI / Native Windows Process Tree | 50 | 2.0/30.0 | 14.0/39.0 | 24.0/28.0 | 11.0/13.0 |
| CI / Public Boundary | 61 | 2.0/3.0 | 1.0/2.0 | 6.0/11.0 | 11.0/14.0 |
| CI / quality | 48 | 2.0/81.0 | 14.0/39.0 | 46.0/59.0 | 73.0/93.0 |
| CI / security | 50 | 2.0/50.0 | 14.0/39.0 | 41.0/49.0 | 319.0/429.0 |
| CI / tests (shard 1 overflow/8) | 50 | 2.0/36.0 | 14.0/39.0 | 21.0/52.0 | 158.0/213.0 |
| CI / tests (shard 1/8) | 50 | 2.0/160.0 | 14.0/39.0 | 21.0/44.0 | 122.0/143.0 |
| CI / tests (shard 2 overflow/8) | 50 | 2.0/48.0 | 14.0/39.0 | 20.0/48.0 | 177.0/214.0 |
| CI / tests (shard 2/8) | 50 | 2.0/12.0 | 14.0/39.0 | 19.0/52.0 | 154.0/190.0 |
| CI / tests (shard 3 overflow/8) | 50 | 2.0/84.0 | 14.0/39.0 | 20.0/46.0 | 210.0/245.0 |
| CI / tests (shard 3/8) | 51 | 2.0/112.0 | 14.0/44.0 | 20.0/45.0 | 148.0/176.0 |
| CI / tests (shard 4/8) | 50 | 2.0/117.0 | 14.0/39.0 | 21.0/43.0 | 192.0/222.0 |
| CI / tests (shard 5/8) | 50 | 2.0/104.0 | 14.0/39.0 | 19.0/46.0 | 231.0/286.0 |
| CI / tests (shard 6 overflow/8) | 50 | 2.0/95.0 | 14.0/39.0 | 21.0/55.0 | 232.0/254.0 |
| CI / tests (shard 6/8) | 50 | 2.0/140.0 | 14.0/39.0 | 19.0/53.0 | 171.0/195.0 |
| CI / tests (shard 7/8) | 50 | 2.0/114.0 | 14.0/39.0 | 19.0/45.0 | 145.0/178.0 |
| CI / tests (shard 8 overflow/8) | 50 | 2.0/65.0 | 14.0/39.0 | 19.0/55.0 | 162.0/200.0 |
| CI / tests (shard 8/8) | 50 | 2.0/72.0 | 14.0/39.0 | 20.0/46.0 | 191.0/206.0 |
| CI / typecheck | 48 | 2.0/22.0 | 14.0/39.0 | 45.0/51.0 | 168.0/219.0 |
| Public Boundary / All Public Refs | 19 | 1.0/2.0 | 1.0/1.0 | 8.0/10.0 | 51.0/70.0 |
| Public Boundary / Public Boundary | 89 | 2.0/3.0 | 1.0/1.0 | 5.0/10.0 | 12.0/14.0 |

## Slowest individual checks

| Step | n successful steps | p50 seconds | p95 seconds |
|---|---:|---:|---:|
| detect-secrets - no new credential material | 48 | 255.0 | 347.0 |
| Run mandatory rendered route canary matrix | 53 | 224.0 | 258.0 |
| Run test-suite shard | 639 | 166.0 | 243.0 |
| Run Windows-relevant application tests with the repository harness | 50 | 163.0 | 241.0 |
| pyright and suppression ceilings — complete active population is exact | 46 | 162.0 | 212.0 |
| Architecture boundary seams and ratchets | 46 | 68.0 | 87.0 |
| Install locked application and test dependencies | 50 | 60.0 | 68.0 |
| Audit all published branch tips | 19 | 48.0 | 58.0 |
| bandit — no new medium+ SAST findings | 49 | 36.0 | 50.0 |
| Install dependencies | 80 | 33.0 | 46.0 |
| Install ruff | 48 | 32.0 | 42.0 |
| Install scanners + runtime deps | 50 | 30.0 | 36.0 |
| Verify canonical design tokens and production Work OS contract | 53 | 30.0 | 41.0 |
| Install runtime dependencies | 2 | 27.0 | 28.0 |
| Install Playwright chromium shell | 53 | 24.0 | 198.0 |

## Turnaround and cost

- CI, all completed non-cancelled PRs: n=30, p50=409.0s, p95=633.0s.
- CI, successful PRs: n=19, p50=399.0s, p95=633.0s.
- Public Boundary, all completed non-cancelled PRs: n=33, p50=21.0s, p95=23.0s.
- Public Boundary, successful PRs: n=33, p50=21.0s, p95=23.0s.
- Observed runner work: Linux 3165.18 minutes; Windows 232.55 minutes. 3397.73 total minutes in 10.08 days. Per-job minute rounding and unobserved startup are separate from these timestamps.
- This public repository uses standard GitHub runners. Compute cost is $0 under current GitHub pricing. This does not establish artifact/cache storage charges or the owner account invoice.
- At unchanged observed minutes, moving all jobs to Blacksmith 8-vCPU Linux ($0.016/min) and Windows ($0.032/min) would cost approximately $173/30 days before free credits and job rounding. This is a planning estimate, not a measured Blacksmith outcome. Half the observed runtime would halve that variable compute estimate. No paid service was enabled.

## Constraints and next changes

- First fix: parallelize the full-tree secret scan on available cores. Preserve all plugins, exclusions, verification policy, and nonzero exits. No scanner substitution.
- Blacksmith supports organizations only. It needs an organization-owned repository, its GitHub App/account installed, and a spending decision. The self-hosted runner API currently lists no registered runners; ephemeral vendor availability cannot be inferred from that response.
- Native merge queue is not available for this personal-account repository. The active ruleset requires CI Gate and an up-to-date branch, with no bypass actors, but has no queue rule. An organization-owned public repository can use GitHub native merge queue. Transfer requires owner choice; no transfer or ruleset change was attempted.
- Do not narrow PR test selection until a full-suite pre-main merge queue is enforced. Current file-shard packing remains unchanged. Full tests, native Windows tests, and browser gates remain required when classified as applicable.
- PR cache observations: Fast Signal environment and SQLite 31/33 hits (93.94%); Design environment 29/30 (96.67%), SQLite 28/30 (93.33%); shard 1 environment and SQLite 32/33 (96.97%). Skipped install/build steps after successful restore identify hits; cancelled/incomplete restores are excluded.
- Main already seeds exact environment caches because the same jobs run on pushes. Shared PR cached-environment hit rate and the SQLite recipe key should be improved without fuzzy environment restores. Quality, typecheck, security and Windows setup still reinstall dependencies each run.
- This is Python/pytest, with a small React sandbox. Ruff/Pyright cannot be replaced by JavaScript-only linters/typecheckers. There is no ESLint, Prettier, Vitest, Nx or Turbo configuration to remove. JavaScript installer/checker changes require a separate measured comparison.
- The installed shared pre-push hook delegates to `.githooks/pre-push`. It checks formatting, lint and suppression rules, but omits types. It also runs tests; the full-test local default cannot be removed until pre-main enforcement exists.

## Sources

- [GitHub merge queue availability and merge_group setup](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue), accessed 2026-11-19.
- [GitHub public-runner billing](https://docs.github.com/en/actions/concepts/billing-and-usage), accessed 2026-11-19.
- [Blacksmith pricing](https://www.blacksmith.sh/pricing), accessed 2026-11-19. Vendor speed claims are not repo benchmarks.

## Reproducible source run IDs

44587377562, 44587377548, 44585801367, 44585801191, 44585787690, 44583227002, 44583226647, 44583212560, 44574585594, 44574585584, 44573237766, 44573237758, 44573223093, 44558984013, 44480039441, 44480039242, 44479909626, 44474259528, 44474259403, 44466877790, 44466877760, 44466873507, 44464291484, 44464291306, 44464263320, 44338912652, 44121819924, 44035987315, 44035987314, 43974307594, 43901249833, 43816586123, 43816586120, 43816582367, 43727724077, 43727724028, 43727697700, 43721854841, 43704271820, 43704271797, 43704269690, 43703817546, 43703817483, 43703806186, 43600582065, 43592582435, 43592582423, 43591564240, 43591564230, 43591560200, 43591027781, 43591027774, 43590960363, 43590867273, 43590712043, 43590712041, 43444673995, 43422160670, 43422160639, 43422155575, 43389406137, 43389406065, 43388290018, 43388289879, 43388283717, 43387965993, 43387965853, 43387956766, 43384960049, 43384959988, 43384956934, 43384946542, 43384946535, 43384854977, 43269921685, 43269921429, 43267870017, 43267869642, 43267856255, 43266201135, 43266200580, 43266169565, 43259404182, 43259404112, 43259359372, 43246706003, 43167404517, 43167404513, 43166599192, 43166599189, 43166593413, 43165076589, 43165076571, 43165045088, 43163834560, 43163834527, 43161932752, 43161932703, 43161913154, 43066218105, 43066218103, 43063756283, 43063756252, 43063744793, 43062229477, 43062229417, 43062218495, 43057614454, 43057614149, 43057602667, 43056451159, 43056450766, 43056387112, 43055617608, 43055617549, 43038697513, 42991530106, 42991529690, 42991524939, 42989660747, 42989660652, 42989636063, 42972341251, 42972341206, 42970464685, 42970464658, 42970421331, 42839305290, 42781107861, 42781107783, 42773447745, 42773447447, 42773442063, 42765778929, 42765778873, 42765742053, 42765742051, 42765738867, 42765439017, 42763770722, 42763770645, 42763768009, 42763391004, 42763390848, 42763378103, 42762679650, 42762679455, 42762654703, 42716635217, 42655090325

## Work sequence

The audit and scanner comparison block the first change. Runner migration and merge-queue enforcement depend on the organization/account decision. Cache keys, hook checks and queue workflow support can be separate changes. Each branch owns its workflow and test edits; no live application or database state is used. This first change is serial because its code and benchmark are small and share the same checkout and CPU.

## First change: full-tree secret scan

The serial hook consumes one core. The CI command now sends 128-file batches to
one worker per available core. It preserves the detector version, plugins,
verification policy, exact exclusions and quality-hash filter. GNU `xargs` returns
a nonzero status when any worker fails. The existing security outcome gate still
requires that scan to succeed.

Local comparison, same 3,672 tracked paths and scanner configuration: serial
205.427 seconds; four workers with size-balanced groups 66.873 seconds; the exact
128-file batching proposed for CI 55.977 seconds. All three found zero issues.
No detection capability was removed. An explicit synthetic password canary was
rejected with the unchanged settings. The executable batching regression checks
302 paths, including spaces and a newline, for exact coverage and a nonzero exit
when a late batch fails. CI measurements are required before calling this a
hosted-runner speed improvement.

Validation: `make check-fast BASE=HEAD` passed; 125 CI-gate tests and 1,226 narrow
secret-filter tests passed. The new batching regression failed before the workflow
edit and passed after it. No test, scanner, retry policy, or timeout was removed or
relaxed. The full suite remains in CI.

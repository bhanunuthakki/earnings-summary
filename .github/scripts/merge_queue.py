"""Serial merge-queue controller for a personal repository without native merge queues.

Membership is the ``merge-queue`` label on an open, same-repository pull request.
The controller selects the oldest entry, takes GitHub's test merge commit C whose
parents are exactly [main B, head H], runs the complete CI suite on C, and lets
trusted finalization publish an App-sourced ``Queue Gate`` status on C before the
workflow fast-forwards main from B to exactly C.

Standard library only: this runs on hosted runners before any dependency install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

LABEL = "merge-queue"
STATUS_CONTEXT = "Queue Gate"
BASE_BRANCH = "main"
CALLER_JOB_NAME = "Full suite"
WORKFLOW_FILE = "merge-queue.yml"
SHARD_LABELS = (
    "1",
    "1 overflow",
    "2",
    "2 overflow",
    "3",
    "3 overflow",
    "4",
    "5",
    "6",
    "6 overflow",
    "7",
    "8",
    "8 overflow",
)
# Exact job inventory of ci.yml in queue mode. tests/test_merge_queue.py keeps
# this in lockstep with the workflow so a dropped or renamed job fails closed.
EXPECTED_CI_JOBS: tuple[str, ...] = (
    "Check Changed Paths",
    "Public Boundary",
    "Fast Signal",
    *(f"tests (shard {label}/8)" for label in SHARD_LABELS),
    "Browser Security",
    "quality",
    "typecheck",
    "security",
    "Native Windows Application Tests",
    "Native Windows Process Tree",
    "CI Gate",
)
READY_ATTEMPTS = 24
READY_DELAY_SECONDS = 5.0

JSON = dict[str, Any]


class Api(Protocol):
    def get(self, path: str) -> Any: ...

    def send(self, method: str, path: str, body: Mapping[str, object] | None = None) -> Any: ...


@dataclass(frozen=True)
class Candidate:
    number: int
    head: str
    base: str
    merge: str


class GitHubApi:
    """Minimal REST client; the token never appears in arguments or output."""

    def __init__(self, token: str, repo: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise SystemExit("::error::missing API token")
        self._token = token
        self.repo = repo
        self._api = api_url.rstrip("/")

    def _request(self, method: str, path: str, body: Mapping[str, object] | None) -> Any:
        url = path if path.startswith("https://") else f"{self._api}/repos/{self.repo}{path}"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if method == "DELETE" and error.code == 404:
                return None
            raise SystemExit(f"::error::{method} {path} failed with HTTP {error.code}") from None
        return json.loads(raw) if raw else None

    def get(self, path: str) -> Any:
        return self._request("GET", path, None)

    def send(self, method: str, path: str, body: Mapping[str, object] | None = None) -> Any:
        return self._request(method, path, body)


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else {}


def _labels(pr: Mapping[str, Any]) -> set[str]:
    raw = pr.get("labels")
    items = cast(list[object], raw) if isinstance(raw, list) else []
    return {str(_mapping(label).get("name", "")) for label in items}


def queued_entries(prs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Durable membership: every open labelled pull request, oldest number first."""

    return sorted((pr for pr in prs if LABEL in _labels(pr)), key=lambda pr: int(pr["number"]))


def eligibility_problem(pr: Mapping[str, Any], repo: str) -> str | None:
    if pr.get("state") != "open":
        return "the pull request is not open"
    if pr.get("draft"):
        return "the pull request is a draft"
    if _mapping(pr.get("base")).get("ref") != BASE_BRANCH:
        return f"the base branch is not {BASE_BRANCH}"
    head_repo = _mapping(_mapping(pr.get("head")).get("repo")).get("full_name")
    if head_repo != repo:
        # Only collaborators can push same-repository heads, which keeps the
        # label an approval of code that outsiders cannot replace.
        return "the head is not a branch of this repository"
    return None


def candidate_state(
    pr: Mapping[str, Any], main_sha: str, merge_parents: Sequence[str] | None
) -> Candidate | str | None:
    """Return a candidate, a dequeue reason, or None while GitHub is still computing."""

    mergeable = pr.get("mergeable")
    if mergeable is False:
        return "the pull request has merge conflicts with main"
    merge = pr.get("merge_commit_sha")
    head = str(_mapping(pr.get("head")).get("sha") or "")
    if mergeable is None or not merge or merge_parents is None:
        return None
    if list(merge_parents) != [main_sha, head]:
        return None
    return Candidate(number=int(pr["number"]), head=head, base=main_sha, merge=str(merge))


def inventory_problems(jobs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Require every expected full-suite job exactly once, each a success."""

    prefix = f"{CALLER_JOB_NAME} / "
    names: Counter[str] = Counter()
    problems: list[str] = []
    for job in jobs:
        name = str(job.get("name", ""))
        if not name.startswith(prefix):
            continue
        short = name.removeprefix(prefix)
        names[short] += 1
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            problems.append(f"{short} finished {job.get('status')}/{job.get('conclusion')}")
    for name in EXPECTED_CI_JOBS:
        if names[name] != 1:
            problems.append(f"{name} ran {names[name]} times; expected exactly once")
    for name in sorted(set(names) - set(EXPECTED_CI_JOBS)):
        problems.append(f"unexpected job {name}")
    return problems


def binding_problems(
    expected: Candidate,
    pr: Mapping[str, Any],
    main_sha: str,
    merge_parents: Sequence[str],
    repo: str,
) -> list[str]:
    problems: list[str] = []
    reason = eligibility_problem(pr, repo)
    if reason:
        problems.append(reason)
    if LABEL not in _labels(pr):
        problems.append("the merge-queue label was removed")
    if _mapping(pr.get("head")).get("sha") != expected.head:
        problems.append("the pull request head changed after selection")
    if main_sha != expected.base:
        problems.append("main moved after selection")
    if list(merge_parents) != [expected.base, expected.head]:
        problems.append("the candidate parents are not exactly [main, head]")
    return problems


def _paginate(api: Api, path: str, key: str | None = None) -> list[JSON]:
    items: list[JSON] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        batch = api.get(f"{path}{separator}per_page=100&page={page}")
        rows = cast(list[JSON], batch[key] if key else batch)
        items.extend(rows)
        if len(rows) < 100:
            return items
        page += 1


def _main_sha(api: Api) -> str:
    return str(api.get(f"/git/ref/heads/{BASE_BRANCH}")["object"]["sha"])


def _parents(api: Api, sha: str) -> list[str]:
    return [str(parent["sha"]) for parent in api.get(f"/git/commits/{sha}")["parents"]]


def _dequeue(api: Api, number: int, message: str) -> None:
    api.send("DELETE", f"/issues/{number}/labels/{LABEL}")
    api.send(
        "POST", f"/issues/{number}/comments", {"body": f"Removed from the merge queue: {message}"}
    )


def _write_outputs(path: Path | None, values: Mapping[str, str]) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    if path is None:
        sys.stdout.write(lines)
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(lines)


def select(
    api: Api,
    repo: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = READY_ATTEMPTS,
) -> Candidate | None:
    prs = _paginate(api, f"/pulls?state=open&base={BASE_BRANCH}&sort=created&direction=asc")
    for entry in queued_entries(prs):
        number = int(entry["number"])
        reason = eligibility_problem(entry, repo)
        if reason:
            _dequeue(api, number, reason)
            continue
        for attempt in range(attempts):
            pr = api.get(f"/pulls/{number}")
            reason = eligibility_problem(pr, repo)
            if reason or LABEL not in _labels(pr):
                break
            merge = pr.get("merge_commit_sha")
            parents = _parents(api, str(merge)) if merge and pr.get("mergeable") else None
            state = candidate_state(pr, _main_sha(api), parents)
            if isinstance(state, Candidate):
                return state
            if isinstance(state, str):
                _dequeue(api, number, state)
                break
            if attempt + 1 < attempts:
                sleep(READY_DELAY_SECONDS)
        else:
            raise SystemExit(f"::error::GitHub did not produce a current test merge for #{number}")
        if reason:
            _dequeue(api, number, reason)
    return None


def finalize(
    api: Api,
    status_api: Api | None,
    repo: str,
    expected: Candidate,
    ci_result: str,
    run_id: str,
    run_attempt: str,
    run_url: str,
) -> bool:
    """Return True only after publishing the App-sourced success status on C."""

    if ci_result != "success":
        _dequeue(
            api,
            expected.number,
            f"the full suite on `{expected.merge}` finished `{ci_result}` ({run_url}). "
            "Fix the cause, then re-apply the label; identical candidates are not rerun.",
        )
        return False
    pr = api.get(f"/pulls/{expected.number}")
    problems = binding_problems(expected, pr, _main_sha(api), _parents(api, expected.merge), repo)
    if problems:
        # A moved main or head yields a new candidate on the next controller run.
        print("::warning::candidate no longer current: " + "; ".join(problems))
        return False
    jobs = _paginate(api, f"/actions/runs/{run_id}/attempts/{run_attempt}/jobs", "jobs")
    missing = inventory_problems(jobs)
    if missing:
        _dequeue(
            api, expected.number, "the full-suite inventory is incomplete: " + "; ".join(missing)
        )
        return False
    if status_api is None:
        raise SystemExit("::error::finalization requires the merge-queue App token")
    status_api.send(
        "POST",
        f"/statuses/{expected.merge}",
        {
            "state": "success",
            "context": STATUS_CONTEXT,
            "description": f"Complete suite passed for #{expected.number}",
            "target_url": run_url,
        },
    )
    return True


def revoke(api: Api, number: int, labels: Sequence[str]) -> bool:
    if LABEL not in labels:
        return False
    _dequeue(api, number, "a new head was pushed. Review it, then re-apply the label.")
    return True


def _candidate_from_env(env: Mapping[str, str]) -> Candidate:
    return Candidate(
        number=int(env["QUEUE_PR"]),
        head=env["QUEUE_HEAD"],
        base=env["QUEUE_BASE"],
        merge=env["QUEUE_MERGE"],
    )


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("select")
    final = sub.add_parser("finalize")
    final.add_argument("--ci-result", required=True)
    sub.add_parser("revoke")
    sub.add_parser("dispatch")
    args = parser.parse_args(argv)

    repo = env["GITHUB_REPOSITORY"]
    api_url = env.get("GITHUB_API_URL", "https://api.github.com")
    api = GitHubApi(env.get("GH_TOKEN", ""), repo, api_url)
    output = Path(env["GITHUB_OUTPUT"]) if env.get("GITHUB_OUTPUT") else None

    if args.command == "select":
        candidate = select(api, repo)
        if candidate is None:
            _write_outputs(output, {"found": "false"})
            return 0
        _write_outputs(
            output,
            {
                "found": "true",
                "pr": str(candidate.number),
                "head": candidate.head,
                "base": candidate.base,
                "merge": candidate.merge,
            },
        )
        return 0
    if args.command == "finalize":
        app_token = env.get("MERGE_QUEUE_APP_TOKEN", "")
        status_api = GitHubApi(app_token, repo, api_url) if app_token else None
        run_url = f"{env['GITHUB_SERVER_URL']}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}"
        merged = finalize(
            api,
            status_api,
            repo,
            _candidate_from_env(env),
            args.ci_result,
            env["GITHUB_RUN_ID"],
            env["GITHUB_RUN_ATTEMPT"],
            run_url,
        )
        _write_outputs(output, {"push": "true" if merged else "false"})
        return 0
    if args.command == "revoke":
        labels = json.loads(env["PR_LABELS"])
        revoke(api, int(env["QUEUE_PR"]), [str(label) for label in labels])
        return 0
    api.send("POST", f"/actions/workflows/{WORKFLOW_FILE}/dispatches", {"ref": BASE_BRANCH})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

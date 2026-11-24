from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "merge_queue.py"
QUEUE = REPO_ROOT / ".github" / "workflows" / "merge-queue.yml"
REVOKE = REPO_ROOT / ".github" / "workflows" / "merge-queue-revoke.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REPO = "owner/repo"
MAIN = "b" * 40
HEAD = "h" * 40
MERGE = "c" * 40


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("merge_queue", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_queue"] = module
    spec.loader.exec_module(module)
    return module


mq = _load()


def _pr(number: int, **overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": number,
        "state": "open",
        "draft": False,
        "labels": [{"name": "merge-queue"}],
        "base": {"ref": "main"},
        "head": {"sha": HEAD, "repo": {"full_name": REPO}},
        "mergeable": True,
        "merge_commit_sha": MERGE,
    }
    pr.update(overrides)
    return pr


class FakeApi:
    def __init__(self, routes: Mapping[str, Any]) -> None:
        self.routes = dict(routes)
        self.sent: list[tuple[str, str, Mapping[str, object] | None]] = []
        self.reads: list[str] = []

    def get(self, path: str) -> Any:
        self.reads.append(path)
        for prefix, value in self.routes.items():
            if path == prefix or path.startswith(prefix + "?"):
                return value() if callable(value) else value
        raise AssertionError(f"unexpected GET {path}")

    def send(self, method: str, path: str, body: Mapping[str, object] | None = None) -> Any:
        self.sent.append((method, path, body))
        return None


def _routes(
    pr: dict[str, Any], *, parents: list[str] | None = None, main: str = MAIN
) -> dict[str, Any]:
    return {
        "/pulls": [pr],
        f"/pulls/{pr['number']}": pr,
        "/git/ref/heads/main": {"object": {"sha": main}},
        f"/git/commits/{MERGE}": {"parents": [{"sha": sha} for sha in (parents or [MAIN, HEAD])]},
    }


def _jobs(
    names: list[str] | None = None,
    overrides: Mapping[str, Mapping[str, str | None]] | None = None,
) -> dict[str, Any]:
    rows = []
    for name in names if names is not None else list(mq.EXPECTED_CI_JOBS):
        row: dict[str, str | None] = {
            "name": f"Full suite / {name}",
            "status": "completed",
            "conclusion": "success",
        }
        row.update((overrides or {}).get(name, {}))
        rows.append(row)
    rows.append({"name": "select", "status": "completed", "conclusion": "success"})
    return {"jobs": rows}


def _no_sleep(_seconds: float) -> None:
    return None


# --- inventory -------------------------------------------------------------


def test_expected_inventory_matches_every_ci_job_in_queue_mode() -> None:
    spec = importlib.util.spec_from_file_location(
        "ci_gate", REPO_ROOT / ".github" / "scripts" / "ci_gate.py"
    )
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert gate.SHARD_LABELS == mq.SHARD_LABELS
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    names: list[str] = []
    for job_id, job in workflow["jobs"].items():
        name = str(job.get("name", job_id))
        if "matrix.label" in name:
            # A candidate's verdict is FULL, so its matrix is every canonical label.
            _, matrix = gate.test_matrix(["FULL\tmerge queue"], None)
            labels = [row["label"] for row in matrix["include"]]
            names.extend(name.replace("${{ matrix.label }}", label) for label in labels)
        else:
            names.append(name)
    assert set(mq.SKIPPED_CI_JOBS) <= set(names)
    assert sorted(set(names) - set(mq.SKIPPED_CI_JOBS)) == sorted(mq.EXPECTED_CI_JOBS)
    assert len(set(mq.EXPECTED_CI_JOBS)) == len(mq.EXPECTED_CI_JOBS)


def test_complete_successful_inventory_has_no_problems() -> None:
    assert mq.inventory_problems(_jobs()["jobs"]) == []


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "failure", None])
def test_any_non_success_job_fails_closed(conclusion: str | None) -> None:
    jobs = _jobs(overrides={"tests (shard 3/8)": {"conclusion": conclusion}})["jobs"]
    assert any("tests (shard 3/8)" in problem for problem in mq.inventory_problems(jobs))


def test_running_job_fails_closed() -> None:
    jobs = _jobs(overrides={"CI Gate": {"status": "in_progress", "conclusion": None}})["jobs"]
    assert mq.inventory_problems(jobs)


def test_main_only_jobs_must_be_skipped_on_a_candidate() -> None:
    skipped = {name: {"conclusion": "skipped"} for name in mq.SKIPPED_CI_JOBS}
    names = [*mq.EXPECTED_CI_JOBS, *mq.SKIPPED_CI_JOBS]
    assert mq.inventory_problems(_jobs(names, skipped)["jobs"]) == []
    ran = mq.inventory_problems(_jobs(names)["jobs"])
    assert [p for p in ran if "must not run" in p] == [
        f"{name} must not run on a candidate" for name in mq.SKIPPED_CI_JOBS
    ]


def test_missing_duplicate_and_unexpected_jobs_fail_closed() -> None:
    names = [name for name in mq.EXPECTED_CI_JOBS if name != "typecheck"]
    names += ["CI Gate", "surprise"]
    problems = mq.inventory_problems(_jobs(names)["jobs"])
    assert any(p.startswith("typecheck ran 0") for p in problems)
    assert any(p.startswith("CI Gate ran 2") for p in problems)
    assert "unexpected job surprise" in problems


# --- selection ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"draft": True}, "draft"),
        ({"state": "closed"}, "not open"),
        ({"base": {"ref": "release"}}, "base branch"),
        ({"head": {"sha": HEAD, "repo": {"full_name": "fork/repo"}}}, "branch of this repository"),
        ({"head": {"sha": HEAD, "repo": None}}, "branch of this repository"),
    ],
)
def test_ineligible_entries_are_rejected(overrides: dict[str, Any], reason: str) -> None:
    problem = mq.eligibility_problem(_pr(1, **overrides), REPO)
    assert problem is not None and reason in problem


def test_queue_order_is_oldest_labelled_number_first() -> None:
    prs = [_pr(9), _pr(3), _pr(5, labels=[{"name": "other"}])]
    assert [pr["number"] for pr in mq.queued_entries(prs)] == [3, 9]


def test_candidate_requires_exact_main_and_head_parents() -> None:
    assert mq.candidate_state(_pr(1), MAIN, [MAIN, HEAD]) == mq.Candidate(1, HEAD, MAIN, MERGE)
    assert mq.candidate_state(_pr(1), MAIN, ["a" * 40, HEAD]) is None
    assert mq.candidate_state(_pr(1), MAIN, [HEAD, MAIN]) is None
    assert mq.candidate_state(_pr(1, mergeable=None), MAIN, None) is None
    assert mq.candidate_state(_pr(1, mergeable=False), MAIN, None) == (
        "the pull request has merge conflicts with main"
    )


def test_select_dequeues_ineligible_entries_and_returns_next_candidate() -> None:
    fork = _pr(2, head={"sha": "f" * 40, "repo": {"full_name": "fork/repo"}})
    good = _pr(4)
    routes = _routes(good)
    routes["/pulls"] = [good, fork]
    api = FakeApi(routes)
    assert mq.select(api, REPO, sleep=_no_sleep) == mq.Candidate(4, HEAD, MAIN, MERGE)
    assert ("DELETE", "/issues/2/labels/merge-queue", None) in api.sent
    assert all("/issues/4/" not in path for _m, path, _b in api.sent)


def test_select_waits_for_github_to_recompute_a_current_test_merge() -> None:
    states = iter([_pr(4, mergeable=None), _pr(4), _pr(4)])
    parents = iter([["a" * 40, HEAD], [MAIN, HEAD]])
    routes = _routes(_pr(4))
    routes["/pulls/4"] = lambda: next(states)
    routes[f"/git/commits/{MERGE}"] = lambda: {"parents": [{"sha": s} for s in next(parents)]}
    sleeps: list[float] = []
    assert mq.select(FakeApi(routes), REPO, sleep=sleeps.append) == mq.Candidate(
        4, HEAD, MAIN, MERGE
    )
    assert len(sleeps) == 2


def test_select_dequeues_conflicts_and_fails_when_merge_never_settles() -> None:
    conflict = FakeApi(_routes(_pr(4, mergeable=False)))
    assert mq.select(conflict, REPO, sleep=_no_sleep) is None
    assert ("DELETE", "/issues/4/labels/merge-queue", None) in conflict.sent

    pending = FakeApi(_routes(_pr(4, mergeable=None)))
    with pytest.raises(SystemExit):
        mq.select(pending, REPO, sleep=_no_sleep, attempts=3)
    assert pending.sent == []


def test_select_with_empty_queue_returns_none() -> None:
    api = FakeApi({"/pulls": [_pr(1, labels=[])]})
    assert mq.select(api, REPO) is None
    assert api.sent == []


# --- finalization --------------------------------------------------------------

EXPECTED = mq.Candidate(4, HEAD, MAIN, MERGE)


def _final_api(pr: dict[str, Any] | None = None, **kwargs: Any) -> FakeApi:
    routes = _routes(pr or _pr(4), **kwargs)
    routes["/actions/runs/77/attempts/1/jobs"] = _jobs()
    return FakeApi(routes)


def _finalize(api: FakeApi, status: FakeApi | None, ci_result: str = "success") -> bool:
    return mq.finalize(api, status, REPO, EXPECTED, ci_result, "77", "1", "https://run")


def test_successful_full_suite_publishes_app_status_on_exact_candidate() -> None:
    api, status = _final_api(), FakeApi({})
    assert _finalize(api, status) is True
    assert status.sent == [
        (
            "POST",
            f"/statuses/{MERGE}",
            {
                "state": "success",
                "context": "Queue Gate",
                "description": "Complete suite passed for #4",
                "target_url": "https://run",
            },
        )
    ]
    assert api.sent == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", ""])
def test_failed_suite_dequeues_once_without_status(result: str) -> None:
    api, status = _final_api(), FakeApi({})
    assert _finalize(api, status, result) is False
    assert status.sent == []
    assert ("DELETE", "/issues/4/labels/merge-queue", None) in api.sent


@pytest.mark.parametrize(
    ("pr", "kwargs"),
    [
        (_pr(4, head={"sha": "d" * 40, "repo": {"full_name": REPO}}), {}),
        (_pr(4, labels=[]), {}),
        (_pr(4), {"main": "e" * 40}),
        (_pr(4), {"parents": [MAIN, "d" * 40]}),
        (_pr(4, state="closed"), {}),
    ],
)
def test_changed_binding_never_publishes(pr: dict[str, Any], kwargs: dict[str, Any]) -> None:
    api, status = _final_api(pr, **kwargs), FakeApi({})
    assert _finalize(api, status) is False
    assert status.sent == []


def test_incomplete_inventory_dequeues_without_status() -> None:
    api, status = _final_api(), FakeApi({})
    api.routes["/actions/runs/77/attempts/1/jobs"] = _jobs(
        [n for n in mq.EXPECTED_CI_JOBS if n != "security"]
    )
    assert _finalize(api, status) is False
    assert status.sent == []
    assert ("DELETE", "/issues/4/labels/merge-queue", None) in api.sent


def test_missing_app_token_fails_instead_of_using_the_actions_token() -> None:
    with pytest.raises(SystemExit):
        _finalize(_final_api(), None)


def test_revoke_only_acts_on_labelled_entries() -> None:
    api = FakeApi({})
    assert mq.revoke(api, 4, ["other"]) is False
    assert api.sent == []
    assert mq.revoke(api, 4, ["merge-queue"]) is True
    assert api.sent[0] == ("DELETE", "/issues/4/labels/merge-queue", None)


def test_api_client_refuses_an_empty_token() -> None:
    with pytest.raises(SystemExit):
        mq.GitHubApi("", REPO)


# --- workflow contracts ----------------------------------------------------------


def _yaml(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    triggers = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(triggers, dict)
    return cast(dict[str, Any], triggers)


def test_controller_is_serial_least_privilege_and_runs_the_complete_suite() -> None:
    wf = _yaml(QUEUE)
    assert set(_on(wf)) == {"pull_request_target", "push", "workflow_dispatch"}
    assert _on(wf)["push"] == {"branches": ["main"]}
    assert wf["concurrency"] == {"group": "merge-queue", "cancel-in-progress": False}
    assert wf["permissions"] == {}
    jobs = wf["jobs"]
    assert "github.ref == 'refs/heads/main'" in jobs["select"]["if"]
    ci = jobs["ci"]
    assert ci["name"] == mq.CALLER_JOB_NAME
    assert ci["uses"] == "./.github/workflows/ci.yml"
    assert ci["cache-mode"] == "read"
    assert ci["permissions"] == {"contents": "read"}
    assert "secrets" not in ci
    assert ci["with"] == {"candidate_sha": "${{ needs.select.outputs.merge }}"}
    for name in ("select", "finalize"):
        assert jobs[name]["cache-mode"] == "none"
        checkout = jobs[name]["steps"][0]
        assert checkout["with"]["ref"] == "${{ github.sha }}"
        assert checkout["with"]["persist-credentials"] is False


def test_finalization_holds_the_only_secret_and_moves_main_with_an_expected_base() -> None:
    final = _yaml(QUEUE)["jobs"]["finalize"]
    assert final["environment"] == "merge-queue"
    assert final["needs"] == ["select", "ci"]
    assert "contents" in final["permissions"] and final["permissions"]["contents"] == "read"
    steps = {step.get("name", step.get("uses")): step for step in final["steps"]}
    app = steps["Mint the merge-queue App token"]
    assert re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", app["uses"])
    assert app["if"] == "${{ needs.ci.result == 'success' }}"
    assert set(app["with"]) == {
        "client-id",
        "private-key",
        "permission-contents",
        "permission-statuses",
        "permission-workflows",
    }
    push = steps["Move main from the tested base to exactly the tested candidate"]["run"]
    assert '--force-with-lease="refs/heads/main:$QUEUE_BASE"' in push
    assert '"$QUEUE_MERGE:refs/heads/main"' in push
    assert 'test "$(git rev-parse "$QUEUE_MERGE^1")" = "$QUEUE_BASE"' in push
    assert "x-access-token@" not in push and "--force " not in push
    text = QUEUE.read_text(encoding="utf-8")
    assert text.count("secrets.") == 1


def test_revocation_never_waits_on_the_controller_or_runs_pr_code() -> None:
    wf = _yaml(REVOKE)
    assert "concurrency" not in wf
    assert _on(wf) == {
        "pull_request_target": {"types": ["synchronize", "reopened", "converted_to_draft"]}
    }
    job = wf["jobs"]["revoke"]
    assert job["steps"][0]["with"] == {
        "ref": "${{ github.event.repository.default_branch }}",
        "persist-credentials": False,
    }
    assert "secrets." not in REVOKE.read_text(encoding="utf-8")


def test_ci_runs_every_job_on_the_exact_candidate_in_queue_mode() -> None:
    wf = _yaml(CI)
    call = _on(wf)["workflow_call"]
    assert call["inputs"]["candidate_sha"]["required"] is True
    assert "inputs.candidate_sha" in wf["concurrency"]["group"]
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["ref"] == "${{ inputs.candidate_sha }}"
                assert step["with"]["persist-credentials"] is False
    classify = wf["jobs"]["changes"]["steps"][1]["run"]
    forced = classify.split('if [ -n "$CANDIDATE_SHA" ]; then', 1)[1].split("fi", 1)[0]
    for group in ("code", "python"):
        assert f'echo "{group}=true"' in forced
    text = CI.read_text(encoding="utf-8")
    assert text.count('base="HEAD^1"') == 2


def test_queue_status_context_is_not_a_job_name_any_workflow_can_emit() -> None:
    for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        for job_id, job in _yaml(path)["jobs"].items():
            assert job.get("name", job_id) != mq.STATUS_CONTEXT

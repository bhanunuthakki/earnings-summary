"""Merge-queue acceptance probe: a candidate that fails only in GitHub Actions.

This file exists only on a throwaway pull request. Do not merge it.
"""

import os


def test_queue_candidate_fails_in_ci() -> None:
    assert os.environ.get("GITHUB_ACTIONS") != "true", "intentional merge-queue probe failure"

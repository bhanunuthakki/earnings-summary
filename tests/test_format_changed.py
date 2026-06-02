"""The diff-aware ``ruff format`` gate (execution/format_changed.py) fails a
change only when a line it actually touched is misformatted — pre-existing
format drift on untouched lines is ignored. Tests the pure diff-parsing +
decision logic directly (the git/ruff shell-outs are thin wrappers).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from execution.format_changed import (  # noqa: E402
    changed_lines,
    file_is_clean,
    reformat_lines,
)


def test_changed_lines_collects_added_new_side() -> None:
    # `git diff -U0` style: one modification hunk + one pure insertion.
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -10 +10 @@\n"
        "-old\n"
        "+new10\n"
        "@@ -20,0 +21,2 @@\n"
        "+new21\n"
        "+new22\n"
    )
    assert changed_lines(diff) == {10, 21, 22}


def test_reformat_lines_collects_rewritten_old_side_ignoring_context() -> None:
    # `ruff format --diff` style WITH context: only the `-` lines (current-file
    # lines ruff rewrites) are collected; context lines are walked, not counted.
    diff = (
        "--- f.py\n"
        "+++ f.py\n"
        "@@ -8,5 +8,4 @@\n"
        " context8\n"
        " context9\n"
        "-    foo(a,\n"
        "-        b)\n"
        "+    foo(a, b)\n"
        " context12\n"
    )
    # context8=8, context9=9, then rewritten old lines 10 and 11.
    assert reformat_lines(diff) == {10, 11}


def test_content_lines_starting_with_dashes_are_not_mistaken_for_headers() -> None:
    # A deleted line whose content begins with "--" must be counted, not skipped
    # as a "--- file" header (headers are only honored before the first hunk).
    diff = "--- f.py\n+++ f.py\n@@ -3,2 +3,1 @@\n---dashed-content\n-    plain\n+merged\n"
    assert reformat_lines(diff) == {3, 4}


def test_clean_when_drift_does_not_touch_changed_lines() -> None:
    # The change touched line 10; ruff wants to rewrite pre-existing drift at
    # lines 40-41 — no overlap, so the gate passes. This is the friction case.
    git_diff = "--- a/f.py\n+++ b/f.py\n@@ -10 +10 @@\n-old\n+new10\n"
    ruff_diff = "--- f.py\n+++ f.py\n@@ -40,2 +40,1 @@\n-drift_a\n-drift_b\n+merged\n"
    clean, offending = file_is_clean(git_diff, ruff_diff)
    assert clean is True
    assert offending == set()


def test_dirty_when_a_changed_line_is_misformatted() -> None:
    # The change touched line 40, which is exactly what ruff wants to rewrite.
    git_diff = "--- a/f.py\n+++ b/f.py\n@@ -40 +40 @@\n-old\n+x=1\n"
    ruff_diff = "--- f.py\n+++ f.py\n@@ -40 +40 @@\n-x=1\n+x = 1\n"
    clean, offending = file_is_clean(git_diff, ruff_diff)
    assert clean is False
    assert offending == {40}


def test_no_ruff_changes_is_always_clean() -> None:
    git_diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n"
    clean, offending = file_is_clean(git_diff, ruff_diff_text="")
    assert clean is True
    assert offending == set()

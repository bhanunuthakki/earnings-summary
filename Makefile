# Developer task runner — encodes the GEMINI.md pre-push checklist as targets so
# CI (.github/workflows/ci.yml) and humans run the same commands.
#
# NOTE on baselines: the repo carries a large pre-existing ruff (~332) and
# pyright-strict (~3070) baseline (tooling drift — see
# directives/regrade_memo_post_wedge.md). So `lint`/`typecheck` over the whole
# tree are INFORMATIONAL; the enforceable gate is "your changed files are
# clean" (lint-changed / typecheck-changed) plus a green test suite. `make
# check` runs exactly what is expected to be green.

.DEFAULT_GOAL := help
PY ?= python
BASE ?= origin/main
# Changed .py files vs BASE, excluding generated migrations and scratch/.
CHANGED = $(shell git diff --name-only --diff-filter=ACMR $(BASE)...HEAD -- '*.py' | grep -vE '^(alembic/versions/|scratch/)')

.PHONY: help install hooks format format-check format-changed lint lint-changed typecheck typecheck-changed test check ci-local

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install dev + runtime deps
	pip install -r requirements.txt && pip install -e .[dev]

hooks:  ## Install pre-commit hooks (commit + pre-push)
	pre-commit install && pre-commit install --hook-type pre-push

format:  ## Auto-format the tree
	ruff format .

format-check:  ## Fail if anything is unformatted (whole tree — informational; ~247-file drift baseline)
	ruff format --check .

format-changed:  ## Format-check only the lines changed vs BASE (the enforceable gate)
	@$(PY) execution/format_changed.py --base $(BASE) $(CHANGED)

lint:  ## Lint the whole tree (informational — has a pre-existing baseline)
	ruff check .

lint-changed:  ## Lint only files changed vs BASE (the enforceable gate)
	@if [ -n "$(CHANGED)" ]; then echo "$(CHANGED)" | xargs ruff check; else echo "no changed .py files"; fi

typecheck:  ## pyright strict over the tree (informational — has a baseline)
	pyright

typecheck-changed:  ## pyright strict on files changed vs BASE (the enforceable gate)
	@if [ -n "$(CHANGED)" ]; then echo "$(CHANGED)" | xargs pyright; else echo "no changed .py files"; fi

test:  ## Run the full test suite
	pytest -q

check: format-changed lint-changed typecheck-changed test  ## Pre-push gate: your-lines format + your-files lint/types + tests

ci-local:  ## Mirror CI locally (format-check on changed + full tests)
	$(MAKE) lint-changed && $(MAKE) test

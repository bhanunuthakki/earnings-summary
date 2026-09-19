# Project task runner — local workflows and CI share retained-file checks.
#
# NOTE on baselines: active Python is Ruff-clean; immutable historical
# migrations retain approved format debt. Pyright and inline suppressions carry
# exact descending subsystem ceilings, while every changed retained file must
# already be wholly clean.

.DEFAULT_GOAL := help

# Fail closed on the project interpreter. An explicit PYTHON_BIN wins; otherwise
# use the checked-out virtual environment. Never fall through to an unrelated
# system `python` whose dependency set may differ from the repository contract.
ifneq ($(strip $(PYTHON_BIN)),)
PY := $(PYTHON_BIN)
else ifneq ($(wildcard .venv/bin/python3),)
PY := .venv/bin/python3
else ifneq ($(wildcard .venv/bin/python),)
PY := .venv/bin/python
else ifneq ($(wildcard .venv/Scripts/python.exe),)
PY := .venv/Scripts/python.exe
else
$(error No project Python found; set PYTHON_BIN or create .venv)
endif
BASE ?= origin/main
PYTEST_WORKERS ?= 2
PYTEST_XDIST_ARGS := $(if $(filter 0,$(PYTEST_WORKERS)),,-n $(PYTEST_WORKERS) --dist=loadfile)
# Inner-loop checks include staged, unstaged, and untracked files.
CHANGE_MODE ?= worktree
CHANGED_CHECK := PYTHONPATH=src $(PY) -m quality.check_changed --base $(BASE) --mode $(CHANGE_MODE)

.PHONY: help install hooks format format-check format-changed lint lint-changed typecheck typecheck-changed suppressions-changed test test-serial test-changed architecture-check instruction-check public-boundary-check public-ref-check check check-fast ci-local

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install dev + runtime deps
	$(PY) -m pip install --require-hashes -r requirements.lock && $(PY) -m pip install -e .[dev]

hooks:  ## Install pre-commit hooks (commit + pre-push)
	pre-commit install && pre-commit install --hook-type pre-push

format:  ## Auto-format active source; preserve immutable historical migrations
	$(PY) -m ruff format --extend-exclude alembic/versions_archived .

format-check:  ## Check whole-tree formatting, including historical migration debt
	$(PY) -m ruff format --check .

format-changed:  ## Require changed retained files to be wholly formatted
	$(CHANGED_CHECK) --check format

lint:  ## Lint the active source under repository configuration
	$(PY) -m ruff check .

lint-changed:  ## Lint only files changed vs BASE (the enforceable gate)
	$(CHANGED_CHECK) --check lint

typecheck:  ## Enforce exact descending whole-tree Pyright and suppression ceilings
	PYTHONPATH=src $(PY) execution/enforce_static_quality.py --pythonpath $(PY)

typecheck-changed:  ## pyright strict on files changed vs BASE (the enforceable gate)
	$(CHANGED_CHECK) --check types

suppressions-changed:  ## Reject inline static-analysis suppressions in changed retained files
	$(CHANGED_CHECK) --check suppressions

test:  ## Run the full test suite
	$(PY) -m pytest -q $(PYTEST_XDIST_ARGS)

test-serial:  ## Run the full suite in one process (lowest local RAM/CPU pressure)
	$(PY) -m pytest -q

test-changed:  ## Run pytest only on changed test files vs BASE
	$(CHANGED_CHECK) --check tests

architecture-check:  ## Guard the monotonic baseline for execution sys.path mutations and loose root src modules
	$(PY) scripts/check_architecture_boundaries.py

instruction-check:  ## Validate layered instructions without app fixtures or DB setup
	$(PY) execution/validate_directive_manifest.py
	$(PY) execution/validate_folder_contract.py
	$(PY) -m pytest -q instruction_tests
	.githooks/test_pre_push.sh

public-boundary-check:  ## Reject private material in the current tracked tree
	$(PY) execution/verify_public_tree.py

public-ref-check:  ## Audit fetched origin branches by private path category
	$(PY) execution/verify_public_tree.py --all-refs

check: architecture-check format-changed lint-changed typecheck-changed typecheck suppressions-changed test  ## Pre-push gate: architecture + format/lint/types/suppressions + tests

check-fast: architecture-check format-changed lint-changed typecheck-changed suppressions-changed test-changed  ## Fast inner-loop gate: architecture + format/lint/types/suppressions + changed-tests

manifest-check:  ## Validate 11-project reconstruction inventory
	$(PY) execution/verify_reconstruction_inventory.py

calendar-check:  ## Validate earnings and research calendars end-to-end
	$(PY) execution/verify_calendars.py

drill: manifest-check calendar-check check-fast  ## Reconstruction drill: inventory + calendars + fast checks

ci-local:  ## Mirror CI locally (format-check on changed + full tests)
	$(MAKE) lint-changed && $(MAKE) test

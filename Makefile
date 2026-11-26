# Project task runner — fast local checks matching CI.

.DEFAULT_GOAL := help

# Fail closed on the project interpreter. An explicit PYTHON_BIN wins; otherwise
# use the checked-out virtual environment.
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

PYTEST_WORKERS ?= 2
PYTEST_XDIST_ARGS := $(if $(filter 0,$(PYTEST_WORKERS)),,-n $(PYTEST_WORKERS) --dist=loadfile)

.PHONY: help install hooks format format-check lint typecheck test test-serial instruction-check public-boundary-check public-ref-check check check-fast manifest-check calendar-check drill

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install:  ## Install dev + runtime deps
	$(PY) -m pip install --require-hashes -r requirements.lock && $(PY) -m pip install -e .[dev]

hooks:  ## Install pre-commit hooks (commit + pre-push)
	pre-commit install && pre-commit install --hook-type pre-push

format:  ## Auto-format active source; preserve historical migrations
	$(PY) -m ruff format --extend-exclude alembic/versions_archived .

format-check:  ## Check formatting on active source
	$(PY) -m ruff format --check --extend-exclude alembic/versions_archived .

lint:  ## Lint active source with Ruff
	$(PY) -m ruff check .

typecheck:  ## Strict Pyright typecheck on src/
	$(PY) -m pyright --pythonpath $(PY) src/

test:  ## Run the full test suite
	$(PY) -m pytest -q $(PYTEST_XDIST_ARGS)

test-serial:  ## Run the full suite in one process
	$(PY) -m pytest -q

instruction-check:  ## Validate layered instructions without app fixtures or DB setup
	$(PY) execution/validate_directive_manifest.py
	$(PY) execution/validate_folder_contract.py
	$(PY) -m pytest -q instruction_tests
	.githooks/test_pre_push.sh

public-boundary-check:  ## Reject private material in the current tracked tree
	$(PY) execution/verify_public_tree.py

public-ref-check:  ## Audit fetched origin branches by private path category
	$(PY) execution/verify_public_tree.py --all-refs

check-fast: format-check lint public-boundary-check instruction-check  ## Fast inner-loop gate

check: format-check lint typecheck public-boundary-check instruction-check test  ## Complete verification gate

manifest-check:  ## Validate reconstruction inventory
	$(PY) execution/verify_reconstruction_inventory.py

calendar-check:  ## Validate earnings and research calendars end-to-end
	$(PY) execution/verify_calendars.py

drill: manifest-check calendar-check check-fast  ## Reconstruction drill

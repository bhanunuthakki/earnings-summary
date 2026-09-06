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
# One-time authority for introducing the first quality receipts. Once those
# receipts exist on BASE this SHA is ignored; any other missing-base state fails.
QUALITY_BOOTSTRAP_BASE := 651a0c9c83062068041e0e62880055669a57eccb
PYTEST_WORKERS ?= 2
PYTEST_XDIST_ARGS := $(if $(filter 0,$(PYTEST_WORKERS)),,-n $(PYTEST_WORKERS) --dist=loadfile)
# Changed .py files vs BASE, excluding generated migrations and scratch/.
CHANGED := $(shell git diff --name-only --diff-filter=ACMR $(BASE)...HEAD -- '*.py' | grep -vE '^(alembic/versions/|scratch/)')
CHANGED_ALL := $(shell git diff --name-only --diff-filter=ACMR $(BASE)...HEAD)
TOUCHED_ARGS := $(foreach path,$(CHANGED_ALL),--touched $(path))

.PHONY: help install hooks format format-check format-changed lint lint-changed typecheck typecheck-changed test test-serial test-changed architecture-check instruction-check public-boundary-check public-ref-check quality-architecture-check quality-duplicates-check quality-reachability-check quality-ratchets check check-fast ci-local

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
	pyright --pythonpath $(PY)

typecheck-changed:  ## pyright strict on files changed vs BASE (the enforceable gate)
	@if [ -n "$(CHANGED)" ]; then echo "$(CHANGED)" | xargs pyright --pythonpath $(PY); else echo "no changed .py files"; fi

test:  ## Run the full test suite
	$(PY) -m pytest -q $(PYTEST_XDIST_ARGS)

test-serial:  ## Run the full suite in one process (lowest local RAM/CPU pressure)
	$(PY) -m pytest -q

test-changed:  ## Run pytest only on changed test files vs BASE
	@changed_tests=$$(git diff --name-only --diff-filter=ACMR $(BASE)...HEAD -- 'tests/test_*.py' 'tests/**/test_*.py' 'instruction_tests/test_*.py' 'instruction_tests/**/test_*.py'); \
	if [ -n "$$changed_tests" ]; then $(PY) -m pytest -q $$changed_tests; else echo "no changed test files"; fi

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

# The first rollout is authorized only from QUALITY_BOOTSTRAP_BASE. Thereafter
# the merge-base receipts are authoritative and a missing receipt fails closed.
quality-architecture-check:  ## Prevent architecture metric regressions
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	base_commit=$$(git rev-parse --verify "$(BASE)^{commit}" 2>/dev/null) || { echo "Invalid quality-ratchet BASE: $(BASE)" >&2; exit 2; }; \
	if git cat-file -e "$(BASE):docs/quality/architecture-ratchet.json" 2>/dev/null; then \
		git show "$(BASE):docs/quality/architecture-ratchet.json" > "$$tmp/architecture-ratchet.json"; \
	elif [ "$$base_commit" = "$(QUALITY_BOOTSTRAP_BASE)" ]; then \
		git show "HEAD:docs/quality/architecture-ratchet.json" > "$$tmp/architecture-ratchet.json"; \
	else \
		echo "Missing BASE-owned architecture ratchet outside the authorized bootstrap" >&2; exit 2; \
	fi; \
	$(PY) execution/score_code_quality.py --repo-root . --revision WORKTREE --baseline "$$tmp/architecture-ratchet.json" --ratchet-only

quality-duplicates-check:  ## Prevent normalized-AST duplication regressions
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	base_commit=$$(git rev-parse --verify "$(BASE)^{commit}" 2>/dev/null) || { echo "Invalid quality-ratchet BASE: $(BASE)" >&2; exit 2; }; \
	if git cat-file -e "$(BASE):docs/quality/duplicates-ratchet.json" 2>/dev/null; then \
		git show "$(BASE):docs/quality/duplicates-ratchet.json" > "$$tmp/duplicates-ratchet.json"; \
	elif [ "$$base_commit" = "$(QUALITY_BOOTSTRAP_BASE)" ]; then \
		git show "HEAD:docs/quality/duplicates-ratchet.json" > "$$tmp/duplicates-ratchet.json"; \
	else \
		echo "Missing BASE-owned duplicate ratchet outside the authorized bootstrap" >&2; exit 2; \
	fi; \
	$(PY) execution/analyze_code_duplicates.py --repo-root . --revision WORKTREE --baseline "$$tmp/duplicates-ratchet.json"

quality-reachability-check:  ## Hold changed files with unknown operational edges
	$(PY) execution/build_operational_reachability.py --repo-root . --output .tmp/quality/reachability-check.json $(TOUCHED_ARGS)

quality-ratchets: quality-architecture-check quality-duplicates-check quality-reachability-check  ## Run Train 0 architecture, clone, and reachability gates

check: architecture-check format-changed lint-changed typecheck-changed quality-ratchets test  ## Pre-push gate: architecture + changed quality ratchets + tests

check-fast: architecture-check format-changed lint-changed typecheck-changed test-changed  ## Fast inner-loop gate: architecture + format/lint/typecheck + changed-tests

manifest-check:  ## Validate 11-project reconstruction inventory
	$(PY) execution/verify_reconstruction_inventory.py

calendar-check:  ## Validate earnings and research calendars end-to-end
	$(PY) execution/verify_calendars.py

drill: manifest-check calendar-check check-fast  ## Reconstruction drill: inventory + calendars + fast checks

ci-local:  ## Mirror CI locally (format-check on changed + full tests)
	$(MAKE) lint-changed && $(MAKE) test

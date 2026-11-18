.DEFAULT_GOAL := help

.PHONY: help install lint test typecheck

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install mise tools, pre-commit hooks and Python dependencies
	mise install
	pre-commit install
	pre-commit install --hook-type commit-msg
	uv sync $(EXTRAS)

# Every extra, so the suite covers every module. pytest is an extra, not a
# dependency group, so `uv run` alone would not install it.
# --locked, as CI runs: fail on a stale uv.lock rather than rewrite it.
EXTRAS := --locked --extra dev --extra azure --extra backup --extra web

test: ## Run the test suite
	uv run $(EXTRAS) pytest

typecheck: ## Type-check src/ and tests/ with mypy (config in pyproject.toml)
	uv run $(EXTRAS) mypy

lint: ## Run all pre-commit hooks against every file
	pre-commit run --all-files

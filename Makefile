.DEFAULT_GOAL := help
.PHONY: help setup fmt lint typecheck test audit hooks check run docker-build docker-run clean

IMAGE ?= service:local
GIT_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo local)

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-13s %s\n", $$1, $$2}'

setup: ## Install locked dependencies and git hooks
	uv sync --locked
	uv run pre-commit install

fmt: ## Format code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Strict type check
	uv run mypy

test: ## Run tests with coverage floor
	uv run pytest --cov

audit: ## Check locked dependencies for known vulnerabilities
	uv export --frozen --no-dev --no-emit-project --format requirements-txt -o .audit-requirements.txt --quiet
	uv run pip-audit --strict --disable-pip --require-hashes -r .audit-requirements.txt
	rm -f .audit-requirements.txt

hooks: ## Run every pre-commit hook against all files
	uv run pre-commit run --all-files

check: lint typecheck test hooks ## Everything CI runs except the Docker build

run: ## Run locally with reload
	uv run uvicorn app.main:app --reload --no-access-log

docker-build: ## Build the production image
	docker build --build-arg GIT_SHA=$(GIT_SHA) -t $(IMAGE) .

docker-run: ## Run the production image on :8000
	docker run --rm -p 8000:8000 --read-only --tmpfs /tmp --cap-drop=ALL --security-opt=no-new-privileges $(IMAGE)

clean: ## Remove caches and build output
	rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov dist build

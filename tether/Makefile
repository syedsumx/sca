# Tether developer commands. `docker compose up -d` first, then `make demo`.
PY ?= python
COMPOSE ?= docker compose

.PHONY: help up down demo test test-docker lint typecheck fmt local-demo migrate seed ui-dev

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

up: ## Build and start db, api, worker and ui
	$(COMPOSE) up -d --build

down: ## Stop everything (keeps the database volume)
	$(COMPOSE) down

demo: ## Run the end-to-end scenario against the running compose stack
	$(COMPOSE) exec -T api tether demo --api-url http://localhost:8000

test-docker: ## Run the pytest suite inside the api container
	$(COMPOSE) exec -T api sh -c "pip install -q pytest && pytest -q tests"

local-demo: ## Run the scenario without Docker (needs Postgres+pgvector at TETHER_DATABASE_URL)
	$(PY) -m tether.cli migrate
	$(PY) -m tether.cli demo --in-process

migrate: ## Apply database migrations
	$(PY) -m tether.cli migrate

seed: ## Reset and seed demo source data
	$(PY) -m tether.cli seed

test: ## Run pytest (needs TETHER_TEST_DATABASE_URL)
	$(PY) -m pytest

lint: ## ruff lint + format check
	$(PY) -m ruff check src tests migrations
	$(PY) -m ruff format --check src tests migrations

fmt: ## Auto-format
	$(PY) -m ruff check --fix src tests migrations
	$(PY) -m ruff format src tests migrations

typecheck: ## mypy (strict)
	$(PY) -m mypy

ui-dev: ## Run the UI dev server on :5173 (proxying the API on :8000)
	cd ui && npm install && npm run dev

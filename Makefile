.PHONY: install test test-live smoke lint doctor up down

install:        ## Create the venv and install everything
	uv sync

test:           ## Unit tests: no keys, no network
	uv run pytest -q

test-live:      ## Tests that need real API keys
	uv run pytest -q -m live

smoke:          ## One full simulated call end to end
	uv run pytest -q -m e2e

lint:
	uv run ruff check . && uv run ruff format --check .

doctor:         ## Check keys and local tooling
	uv run gf doctor

up:             ## Start every service
	docker compose up --build

down:
	docker compose down --remove-orphans

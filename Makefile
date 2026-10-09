.PHONY: install test test-ui test-live smoke lint doctor up down ui demo report sample-report

install:        ## Create the venv and install everything
	uv sync

test:           ## Unit tests: no keys, no network
	uv run pytest -q

test-ui:        ## Dashboard browser test (Playwright + Chromium): every button, tab, player and link
	uv run playwright install chromium
	uv run pytest -q -m ui tests/ui

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

ui:             ## Live UI on http://127.0.0.1:8090 (reads runs/ and sessions/)
	uv run gf ui

demo:           ## Seed runs/demo from the committed real call records (no keys needed)
	uv run gf demo-run

report:         ## Static report folder for a run: make report RUN=full-1
	uv run gf report $(RUN)

sample-report:  ## Bundle a run (MP3 audio) into docs/sample-report: make sample-report RUN=full-1
	rm -rf docs/sample-report && uv run gf report $(RUN) --bundle --out docs/sample-report

PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin
FAKE_STORE := http://127.0.0.1:8100/api/v1/stores/ica-teststad

.DEFAULT_GOAL := help
.PHONY: help install test lint acceptance dev run

help:
	@echo 'make install      Create .venv and install the project with dev tools'
	@echo 'make test         Unit tests (no network needed)'
	@echo 'make lint         Ruff'
	@echo 'make acceptance   Acceptance tests: the service against the fake store, in one process'
	@echo 'make dev          Fake store (port 8100) and the service; dashboard on http://localhost:8000'
	@echo 'make run          The service against the store in .env (STORE_API_URL, STORE_API_TOKEN)'

$(BIN)/pytest: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet -e ".[dev]"
	@touch $(BIN)/pytest

install: $(BIN)/pytest

test: install
	$(BIN)/pytest

lint: install
	$(BIN)/ruff check .

acceptance: install
	$(BIN)/pytest acceptance -v

# Both stop with Ctrl-C. A separate database keeps local experiments apart from the real store.
dev: install
	@trap 'kill 0' INT TERM EXIT; \
	$(BIN)/python -m storecloud.fakestore & \
	STORE_API_URL=$(FAKE_STORE) STORE_API_TOKEN=dev-token DATABASE_URL=sqlite:///dev.db \
		$(BIN)/uvicorn --factory storecloud.app:create_app --port 8000

run: install
	$(BIN)/uvicorn --factory storecloud.app:create_app --port 8000

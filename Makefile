.PHONY: install dev test lint typecheck validate run postgres-up postgres-down

install:
	python -m pip install -e .

dev:
	python -m pip install -e '.[dev]'

test:
	pytest

lint:
	ruff check .

typecheck:
	mypy apps orchestrator workers execution

validate:
	python scripts/validate_contracts.py

run:
	uvicorn apps.api.app.main:app --reload

postgres-up:
	docker compose up -d postgres

postgres-down:
	docker compose down

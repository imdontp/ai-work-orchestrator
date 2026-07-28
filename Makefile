.PHONY: install dev test test-live spike lint typecheck validate run postgres-up postgres-down

install:
	python -m pip install -e .

dev:
	python -m pip install -e '.[dev]'

test:
	pytest

# Invokes the real Claude Code and Codex CLIs. Spends subscription quota and needs an
# authenticated machine, so it is deliberately not part of `make test`.
test-live:
	AIWO_LIVE_TESTS=1 pytest tests/test_worker_adapters_live.py -v

# Re-record CLI behaviour after an upgrade. Needs a disposable repo to work in.
spike:
	python scripts/spike_m1.py --sandbox $(SANDBOX) --suite all

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

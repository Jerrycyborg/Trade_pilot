UV ?= uv

.PHONY: setup lint test run-policy run-execution run-strategy aahp-validate aahp-checksums

setup:
	$(UV) sync --all-packages --group dev

lint:
	$(UV) run --group dev ruff check .

test:
	$(UV) run --group dev pytest

run-policy:
	$(UV) run uvicorn policy_service.main:app --host 0.0.0.0 --port 8001

run-execution:
	$(UV) run uvicorn execution_service.main:app --host 0.0.0.0 --port 8002

run-strategy:
	$(UV) run uvicorn strategy_service.main:app --host 0.0.0.0 --port 8003

run:
	@echo "Run one service at a time with make run-policy, run-execution, or run-strategy"

aahp-validate:
	python3 tools/aahp.py validate-manifest

aahp-checksums:
	python3 tools/aahp.py generate-checksums

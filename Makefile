UV ?= uv

.PHONY: setup lint test \
	run-research run-strategy run-policy run-execution run-portfolio run-dashboard \
	run-audit run-orchestrator run-sentiment run-notification run-approval \
	aahp-validate aahp-checksums

setup:
	$(UV) sync --all-packages --group dev

lint:
	$(UV) run --group dev ruff check .

test:
	$(UV) run --group dev pytest

run-research:
	$(UV) run uvicorn research_service.main:app --host 0.0.0.0 --port 8005

run-strategy:
	$(UV) run uvicorn strategy_service.main:app --host 0.0.0.0 --port 8003

run-policy:
	$(UV) run uvicorn policy_service.main:app --host 0.0.0.0 --port 8001

run-execution:
	$(UV) run uvicorn execution_service.main:app --host 0.0.0.0 --port 8002

run-portfolio:
	$(UV) run uvicorn portfolio_service.main:app --host 0.0.0.0 --port 8004

run-audit:
	$(UV) run uvicorn audit_logger.main:app --host 0.0.0.0 --port 8006

run-orchestrator:
	$(UV) run uvicorn autonomy_orchestrator.main:app --host 0.0.0.0 --port 8007

run-sentiment:
	$(UV) run uvicorn sentiment_aggregator.main:app --host 0.0.0.0 --port 8008

run-notification:
	$(UV) run uvicorn notification_service.main:app --host 0.0.0.0 --port 8009

run-approval:
	$(UV) run uvicorn approval_gateway.main:app --host 0.0.0.0 --port 8010

run-dashboard:
	python3 -m http.server 8080 --directory apps/dashboard

run:
	@echo "Start services individually:"
	@echo "  make run-research   (port 8005)"
	@echo "  make run-strategy   (port 8003)"
	@echo "  make run-policy     (port 8001)"
	@echo "  make run-execution  (port 8002)"
	@echo "  make run-portfolio  (port 8004)"
	@echo "  make run-audit      (port 8006)"
	@echo "  make run-orchestrator (port 8007)"
	@echo "  make run-sentiment  (port 8008)"
	@echo "  make run-notification (port 8009)"
	@echo "  make run-approval   (port 8010)"
	@echo "  make run-dashboard  (port 8080)"

aahp-validate:
	python3 tools/aahp.py validate-manifest

aahp-checksums:
	python3 tools/aahp.py generate-checksums

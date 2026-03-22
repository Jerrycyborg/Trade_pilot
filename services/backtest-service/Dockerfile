FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./
COPY libs/ ./libs/
COPY services/backtest-service/ ./services/backtest-service/

RUN cd services/backtest-service && uv sync --no-dev

EXPOSE 8009

CMD ["uv", "run", "--directory", "services/backtest-service", "uvicorn", "backtest_service.main:app", "--host", "0.0.0.0", "--port", "8009"]

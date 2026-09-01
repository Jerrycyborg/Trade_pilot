FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./
COPY libs/ ./libs/
COPY services/backtest-service/ ./services/backtest-service/

RUN cd services/backtest-service && uv sync --no-dev

EXPOSE 8011

CMD ["uv", "run", "--directory", "services/backtest-service", "uvicorn", "backtest_service.main:app", "--host", "0.0.0.0", "--port", "8011"]

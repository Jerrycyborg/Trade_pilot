FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir uv && uv sync --all-packages

CMD ["uv", "run", "uvicorn", "strategy_service.main:app", "--host", "0.0.0.0", "--port", "8003"]

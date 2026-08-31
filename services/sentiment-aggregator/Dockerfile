FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir uv && uv sync --all-packages

EXPOSE 8008

CMD ["uv", "run", "uvicorn", "sentiment_aggregator.main:app", "--host", "0.0.0.0", "--port", "8008"]

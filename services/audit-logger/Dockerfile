FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir uv && uv sync --all-packages

EXPOSE 8006

CMD ["uv", "run", "uvicorn", "audit_logger.main:app", "--host", "0.0.0.0", "--port", "8006"]

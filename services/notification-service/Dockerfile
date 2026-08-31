FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir uv && uv sync --all-packages

EXPOSE 8009

CMD ["uv", "run", "uvicorn", "notification_service.main:app", "--host", "0.0.0.0", "--port", "8009"]

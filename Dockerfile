FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY apps ./apps
COPY docs ./docs
COPY scripts ./scripts
RUN mkdir -p /app/artifacts /app/data/cache

EXPOSE 8000
CMD ["uvicorn", "ai_quant_assistant.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]


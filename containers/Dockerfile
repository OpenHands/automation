FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONPATH='/app'

# Datadog labels
LABEL com.datadoghq.tags.service="automation"

# Install system deps for asyncpg + uv
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv ddtrace

# Copy everything and install
COPY pyproject.toml ./
COPY README.md ./
COPY automation/ automation/
COPY migrations/ migrations/
COPY alembic.ini .

RUN uv pip install --system .

# Security: run as non-root (UID 42420 chosen arbitrarily)
RUN groupadd -r automation && useradd -r -g automation -u 42420 automation
USER automation

EXPOSE 8000

CMD ["ddtrace-run", "uvicorn", "automation.app:app", "--host", "0.0.0.0", "--port", "8000"]

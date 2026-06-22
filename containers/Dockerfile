# ---- Stage 1: Build frontend ----
FROM node:24-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# ---- Stage 2: Python application ----
FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONPATH='/app'

# Datadog labels
LABEL com.datadoghq.tags.service="automation"

# Install system deps for asyncpg + uv + git (for SDK git dependencies)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev git && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv ddtrace

# Copy everything and install
COPY pyproject.toml ./
COPY README.md ./
COPY openhands/ openhands/
COPY migrations/ migrations/
COPY alembic.ini .

RUN uv pip install --system .

# Copy built frontend assets from stage 1
COPY --from=frontend-build /frontend/build /app/frontend-dist

# Security: run as non-root (UID 42420 chosen arbitrarily)
RUN groupadd -r automation && useradd -r -g automation -u 42420 automation
USER automation

# Enable frontend hosting — set to "" to serve API only
ENV AUTOMATION_FRONTEND_DIR=/app/frontend-dist

EXPOSE 8000

CMD ["ddtrace-run", "uvicorn", "openhands.automation.app:app", "--host", "0.0.0.0", "--port", "8000"]

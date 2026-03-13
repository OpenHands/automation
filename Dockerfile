FROM python:3.12-slim

# Security: run as non-root
RUN groupadd -r automation && useradd -r -g automation -u 42420 automation

WORKDIR /app

# Install system deps for asyncpg
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-dev . && \
    pip uninstall -y uv

# Copy application code
COPY automation/ automation/
COPY migrations/ migrations/
COPY alembic.ini .

USER automation

EXPOSE 8000

CMD ["uvicorn", "automation.app:app", "--host", "0.0.0.0", "--port", "8000"]

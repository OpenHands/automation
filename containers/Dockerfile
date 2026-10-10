FROM python:3.12-slim-trixie

ARG DEBIAN_SNAPSHOT=20261009T000000Z

WORKDIR /app
ENV PYTHONPATH='/app'

# Datadog labels
LABEL com.datadoghq.tags.service="automation"

COPY containers/constraints-1.14.0.txt containers/constraints-1.14.0.txt

# Install uv + git (for SDK git dependencies)
RUN printf '%s\n' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
        'Suites: trixie' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/" \
        'Suites: trixie-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        > /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends git && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir -c /app/containers/constraints-1.14.0.txt uv ddtrace

# Copy everything and install
COPY pyproject.toml ./
COPY README.md ./
COPY openhands/ openhands/
COPY migrations/ migrations/
COPY alembic.ini .

RUN uv pip install --system -c containers/constraints-1.14.0.txt .

# Security: run as non-root (UID 42420 chosen arbitrarily)
RUN groupadd -r automation && useradd -r -g automation -u 42420 automation
USER automation

EXPOSE 8000

CMD ["ddtrace-run", "uvicorn", "openhands.automation.app:app", "--host", "0.0.0.0", "--port", "8000"]

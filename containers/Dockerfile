FROM python:3.12-slim-trixie AS snapshot-base

ARG DEBIAN_SNAPSHOT=20261112T000000Z
# snapshot.debian.org intermittently returns 5xx under load; retry with
# backoff like the agent-server image (SDK #6502) so a single 503 does not
# fail the whole build.
ARG APT_OPTS="-o Acquire::Retries=5 -o Acquire::http::Timeout=120 -o Acquire::https::Timeout=120 -o Acquire::Retry-Delay=10 -o Acquire::Queue-Mode=access -o Acquire::Pipeline-Depth=0"

RUN printf '%s\n' \
        'Types: deb' \
        "URIs: https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
        'Suites: trixie' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        "URIs: https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/" \
        'Suites: trixie-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        > /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' \
        'Acquire::Retries "5";' \
        'Acquire::http::Timeout "120";' \
        'Acquire::https::Timeout "120";' \
        'Acquire::Retry-Delay "10";' \
        'Acquire::Queue-Mode "access";' \
        'Acquire::Pipeline-Depth "0";' \
        > /etc/apt/apt.conf.d/80-snapshot-resilience && \
    for attempt in 1 2 3 4 5; do \
        apt-get ${APT_OPTS} update \
            && apt-get install -y --no-install-recommends git \
            && apt-get upgrade -y --no-install-recommends \
            && break; \
        echo "apt failed (attempt ${attempt}/5), retrying in 10s..."; \
        sleep 10; \
    done; \
    apt-get clean && rm -rf /var/lib/apt/lists/*

FROM snapshot-base AS builder

ARG UV_VERSION=0.13.5

WORKDIR /app

# Upgrade pip first: the base image's pip vendors an old certifi bundle that
# still trusts the compromised eMudhra roots (GHSA-6q48-6c7w-9gw2), so it must
# not be the tool that downloads uv.
# Disable truststore for the uv download: the pinned Debian store still includes
# the affected root, so unioning it with pip's fixed bundle would restore trust.
RUN pip install --no-cache-dir "pip>=26.2.2" \
    && pip install --use-deprecated=legacy-certs --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock ./
COPY README.md ./
COPY openhands/ openhands/
COPY migrations/ migrations/
COPY alembic.ini .

RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --locked --no-dev --group production \
    --no-editable --python /usr/local/bin/python --no-python-downloads

FROM snapshot-base AS runtime

WORKDIR /app
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app"
# The service image runs behind an ingress. Direct deployments can override
# this with their trusted proxy IPs or CIDRs.
ENV FORWARDED_ALLOW_IPS='*'

LABEL com.datadoghq.tags.service="automation"

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/migrations /app/migrations
COPY --from=builder /app/alembic.ini /app/alembic.ini

RUN /usr/local/bin/python -m pip uninstall --yes pip && \
    groupadd -r automation && useradd -r -g automation -u 42420 automation && \
    mkdir /data && chown automation:automation /data
USER automation

EXPOSE 8000

CMD ["ddtrace-run", "uvicorn", "openhands.automation.app:app", "--host", "0.0.0.0", "--port", "8000"]

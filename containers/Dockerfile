FROM python:3.12-slim-trixie AS snapshot-base

ARG DEBIAN_SNAPSHOT=20260930T000000Z

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
    apt-get clean && rm -rf /var/lib/apt/lists/*

FROM snapshot-base AS builder

ARG UV_VERSION=0.12.23

WORKDIR /app

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml ./
COPY README.md ./
COPY openhands/ openhands/
COPY migrations/ migrations/
COPY alembic.ini .

RUN uv venv /opt/venv --python /usr/local/bin/python --no-python-downloads && \
    uv pip install --python /opt/venv/bin/python . ddtrace

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
    groupadd -r automation && useradd -r -g automation -u 42420 automation
USER automation

EXPOSE 8000

CMD ["ddtrace-run", "uvicorn", "openhands.automation.app:app", "--host", "0.0.0.0", "--port", "8000"]

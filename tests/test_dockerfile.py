from pathlib import Path


REPOSITORY = Path(__file__).parents[1]
DOCKERFILE = REPOSITORY / "containers" / "Dockerfile"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_runtime_uses_snapshot_base_without_package_managers() -> None:
    dockerfile = _dockerfile()

    assert "FROM python:3.12-slim-trixie AS snapshot-base" in dockerfile
    assert "FROM snapshot-base AS builder" in dockerfile
    assert "FROM snapshot-base AS runtime" in dockerfile
    assert "COPY --from=builder /opt/venv /opt/venv" in dockerfile
    assert "/usr/local/bin/python -m pip uninstall --yes pip" in dockerfile
    assert 'PATH="/opt/venv/bin:${PATH}"' in dockerfile


def test_runtime_contract_is_preserved() -> None:
    dockerfile = _dockerfile()

    assert "apt-get install -y --no-install-recommends git" in dockerfile
    assert "COPY --from=builder /app/migrations /app/migrations" in dockerfile
    assert "COPY --from=builder /app/alembic.ini /app/alembic.ini" in dockerfile
    assert "useradd -r -g automation -u 42420 automation" in dockerfile
    assert "USER automation" in dockerfile
    assert (
        'CMD ["ddtrace-run", "uvicorn", "openhands.automation.app:app", '
        '"--host", "0.0.0.0", "--port", "8000"]'
    ) in dockerfile


def test_runtime_data_dir_is_owned_by_service_user() -> None:
    dockerfile = _dockerfile()
    data_dir = "mkdir /data && chown automation:automation /data"

    assert data_dir in dockerfile
    assert dockerfile.index(data_dir) < dockerfile.index("USER automation")
    assert not any(line.startswith("VOLUME") for line in dockerfile.splitlines())

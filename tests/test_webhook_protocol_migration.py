"""Upgrade existing webhook rows and round-trip protocol options on both DBs."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]


def migrate(url, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "AUTOMATION_DB_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_legacy_webhook_upgrade_and_downgrade(backend, request, tmp_path):
    if backend == "sqlite":
        url = f"sqlite:///{tmp_path / 'webhook-migration.db'}"
    else:
        url = (
            request.getfixturevalue("postgres_container")
            .get_connection_url()
            .replace("postgresql+psycopg2://", "postgresql+pg8000://")
        )
    engine = create_engine(url, hide_parameters=True)
    columns = {"event_id_expr", "admission_expr", "acknowledgement"}
    try:
        migrate(url, "upgrade", "037")
        webhook_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO custom_webhooks "
                    "(id,org_id,name,source,webhook_secret,enabled,"
                    "event_key_expr,signature_header) "
                    "VALUES (:id,:org,'Legacy','legacy',"
                    "'synthetic-test-secret',true,'type','X-Signature')"
                ),
                {"id": webhook_id.hex, "org": uuid.uuid4().hex},
            )
        migrate(url, "upgrade", "head")
        assert columns <= {
            column["name"] for column in inspect(engine).get_columns("custom_webhooks")
        }
        with engine.begin() as conn:
            assert conn.execute(
                text(
                    "SELECT event_id_expr, admission_expr, acknowledgement "
                    "FROM custom_webhooks"
                )
            ).one() == (None, None, None)
            conn.execute(
                text(
                    "UPDATE custom_webhooks SET event_id_expr='id', "
                    "admission_expr='allowed', acknowledgement='event_response'"
                )
            )
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT event_id_expr, admission_expr, acknowledgement "
                    "FROM custom_webhooks"
                )
            ).one() == ("id", "allowed", "event_response")
        migrate(url, "downgrade", "037")
        assert not columns & {
            column["name"] for column in inspect(engine).get_columns("custom_webhooks")
        }
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT source FROM custom_webhooks")).scalar_one()
                == "legacy"
            )
    finally:
        engine.dispose()
        migrate(url, "downgrade", "base")

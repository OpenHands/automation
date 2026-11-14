"""Migration 038: factories and automations.factory_id, with data in place."""

import os
import sqlite3
import subprocess
import uuid

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from openhands.automation.models import Automation, AutomationRun


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic(db_url: str, *args: str) -> str:
    env = os.environ.copy()
    env["AUTOMATION_DB_URL"] = db_url
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, f"alembic {args} failed: {result.stderr}"
    return result.stdout + result.stderr


def test_existing_rows_survive_the_upgrade_with_no_factory(tmp_path):
    db_path = str(tmp_path / "factories.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "037")
    automation_id = uuid.uuid4()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO automations (id, user_id, created_by_user_id, org_id, name,"
            " trigger, tarball_path, entrypoint, enabled)"
            " VALUES (?,?,?,?,?,?,?,?,1)",
            (
                automation_id.hex,
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                "legacy",
                "{}",
                "s3://bucket/code.tar.gz",
                "run",
            ),
        )
        con.execute(
            "INSERT INTO automation_runs (id, automation_id, status, trigger_source)"
            " VALUES (?,?,?,?)",
            (uuid.uuid4().hex, automation_id.hex, "COMPLETED", "manual"),
        )
    _alembic(db_url, "upgrade", "head")

    engine = create_engine(db_url)
    with Session(engine) as session:
        automation = session.scalars(select(Automation)).one()
        runs = session.scalars(select(AutomationRun)).all()
    inspector = inspect(engine)
    indexes = {index["name"] for index in inspector.get_indexes("automations")}
    foreign_keys = inspector.get_foreign_keys("automations")
    factory_columns = {c["name"] for c in inspector.get_columns("factories")}
    engine.dispose()

    assert automation.factory_id is None
    assert len(runs) == 1
    assert "ix_automations_factory_id" in indexes
    assert [
        (fk["referred_table"], fk.get("options", {}).get("ondelete"))
        for fk in foreign_keys
    ] == [("factories", "SET NULL")]
    assert factory_columns == {"id", "org_id", "name", "created_at", "updated_at"}


def test_the_upgrade_adds_one_head_and_downgrades(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'heads.db'}"
    _alembic(db_url, "upgrade", "head")

    assert _alembic(db_url, "heads").count("(head)") == 1
    _alembic(db_url, "downgrade", "037")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    assert "factories" not in inspector.get_table_names()
    assert "factory_id" not in {c["name"] for c in inspector.get_columns("automations")}
    engine.dispose()

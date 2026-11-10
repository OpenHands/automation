"""Migration 038: existing automations keep working, and the foreign key and
downgrade behave."""

import os
import sqlite3
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from openhands.automation.models import Automation


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic(db_url: str, *args: str) -> None:
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


def _insert_automation(con: sqlite3.Connection, automation_id: uuid.UUID) -> None:
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


def test_legacy_automation_belongs_to_no_factory(tmp_path):
    db_path = str(tmp_path / "factories.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "037")
    with sqlite3.connect(db_path) as con:
        _insert_automation(con, uuid.uuid4())

    _alembic(db_url, "upgrade", "head")

    engine = create_engine(db_url)
    with Session(engine) as session:
        automation = session.scalars(select(Automation)).one()
    engine.dispose()
    assert automation.factory_id is None


def test_deleting_a_factory_row_clears_the_foreign_key(tmp_path):
    db_path = str(tmp_path / "factories.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "head")
    automation_id, factory_id = uuid.uuid4(), uuid.uuid4()
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            "INSERT INTO factories (id, org_id, name, slug) VALUES (?,?,?,?)",
            (factory_id.hex, uuid.uuid4().hex, "F", "f"),
        )
        _insert_automation(con, automation_id)
        con.execute(
            "UPDATE automations SET factory_id = ? WHERE id = ?",
            (factory_id.hex, automation_id.hex),
        )
        con.execute("DELETE FROM factories WHERE id = ?", (factory_id.hex,))
        remaining = con.execute(
            "SELECT factory_id FROM automations WHERE id = ?", (automation_id.hex,)
        ).fetchone()

    assert remaining == (None,)


def test_factory_slug_is_unique_per_org(tmp_path):
    db_path = str(tmp_path / "factories.db")
    _alembic(f"sqlite:///{db_path}", "upgrade", "head")
    org_id = uuid.uuid4().hex
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO factories (id, org_id, name, slug) VALUES (?,?,?,?)",
            (uuid.uuid4().hex, org_id, "F", "f"),
        )
        con.execute(
            "INSERT INTO factories (id, org_id, name, slug) VALUES (?,?,?,?)",
            (uuid.uuid4().hex, uuid.uuid4().hex, "F", "f"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO factories (id, org_id, name, slug) VALUES (?,?,?,?)",
                (uuid.uuid4().hex, org_id, "Again", "f"),
            )


def test_downgrade_removes_factories(tmp_path):
    db_path = str(tmp_path / "factories.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "head")

    _alembic(db_url, "downgrade", "037")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("automations")}
    tables = set(inspector.get_table_names())
    engine.dispose()
    assert "factory_id" not in columns
    assert "factories" not in tables

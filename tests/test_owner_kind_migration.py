"""Migration 035: legacy rows must load through the ORM after the upgrade."""

import os
import sqlite3
import subprocess
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from openhands.automation.models import Automation, AutomationOwnerKind


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


def test_legacy_row_backfills_to_a_loadable_user_owner(tmp_path):
    db_path = str(tmp_path / "owner-kind.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "034")
    automation_id = uuid.uuid4()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO automations (id, user_id, org_id, name, trigger,"
            " tarball_path, entrypoint, enabled) VALUES (?,?,?,?,?,?,?,1)",
            (
                automation_id.hex,
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                "legacy",
                "{}",
                "s3://bucket/code.tar.gz",
                "run",
            ),
        )

    _alembic(db_url, "upgrade", "head")

    engine = create_engine(db_url)
    with Session(engine) as session:
        loaded = session.scalars(select(Automation)).one()
    engine.dispose()
    assert loaded.owner_kind is AutomationOwnerKind.USER

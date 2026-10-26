"""Migration 036: the change-event table is created on a database with legacy rows."""

import os
import sqlite3
import subprocess
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from openhands.automation.models import (
    AutomationChangeActorKind,
    AutomationChangeEvent,
)


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


def test_change_events_attach_to_a_legacy_automation(tmp_path):
    db_path = str(tmp_path / "change-events.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "035")
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
        session.add(
            AutomationChangeEvent(
                automation_id=automation_id,
                actor_kind=AutomationChangeActorKind.GIT_SYNC,
                action="git_sync_update",
                changed_fields=["name"],
            )
        )
        session.commit()
        loaded = session.scalars(select(AutomationChangeEvent)).one()
    engine.dispose()
    assert loaded.actor_user_id is None
    assert loaded.created_at is not None

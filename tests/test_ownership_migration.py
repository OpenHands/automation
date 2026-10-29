"""Migration 036: legacy rows load through the ORM and accept change events."""

import os
import sqlite3
import subprocess
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from openhands.automation.models import (
    Automation,
    AutomationChangeActorKind,
    AutomationChangeEvent,
    AutomationOwnerKind,
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


def _upgrade_with_legacy_row(tmp_path) -> tuple[str, uuid.UUID, uuid.UUID]:
    """Insert an automation at revision 035, then upgrade to head."""
    db_path = str(tmp_path / "ownership.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "035")
    automation_id = uuid.uuid4()
    org_id = uuid.uuid4()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO automations (id, user_id, org_id, name, trigger,"
            " tarball_path, entrypoint, enabled) VALUES (?,?,?,?,?,?,?,1)",
            (
                automation_id.hex,
                uuid.uuid4().hex,
                org_id.hex,
                "legacy",
                "{}",
                "s3://bucket/code.tar.gz",
                "run",
            ),
        )
    _alembic(db_url, "upgrade", "head")
    return db_url, automation_id, org_id


def test_legacy_row_backfills_to_a_loadable_user_owner(tmp_path):
    db_url, _, _ = _upgrade_with_legacy_row(tmp_path)

    engine = create_engine(db_url)
    with Session(engine) as session:
        loaded = session.scalars(select(Automation)).one()
    engine.dispose()
    assert loaded.owner_kind is AutomationOwnerKind.USER


def test_change_events_attach_to_a_legacy_automation(tmp_path):
    db_url, automation_id, org_id = _upgrade_with_legacy_row(tmp_path)

    engine = create_engine(db_url)
    with Session(engine) as session:
        session.add(
            AutomationChangeEvent(
                automation_id=automation_id,
                org_id=org_id,
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

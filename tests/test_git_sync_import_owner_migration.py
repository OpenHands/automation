"""Migration 037: an org config saved before it loads with no import owner."""

import os
import sqlite3
import subprocess
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from openhands.automation.models import AutomationGitSyncOrgConfig


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


def test_legacy_org_config_has_no_import_owner(tmp_path):
    db_path = str(tmp_path / "import-owner.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "036")
    org_id = uuid.uuid4()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO automation_git_sync_org_config (org_id, overrides)"
            " VALUES (?, '{}')",
            (org_id.hex,),
        )

    _alembic(db_url, "upgrade", "head")

    engine = create_engine(db_url)
    with Session(engine) as session:
        loaded = session.scalars(select(AutomationGitSyncOrgConfig)).one()
    engine.dispose()
    assert loaded.org_id == org_id
    assert loaded.import_owner_user_id is None

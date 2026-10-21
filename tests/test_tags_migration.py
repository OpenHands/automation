"""Migration 034 gives existing automations an empty tag list.

Builds the schema through 033 in a temporary SQLite database, inserts an
automation that predates tags, and upgrades: the row must read back ``[]``
(the tag facet query cannot expand a NULL), and the downgrade must drop the
column again.
"""

import json
import os
import sqlite3
import subprocess
import uuid


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


def _insert_pre_tags_automation(db_path: str) -> str:
    """Insert a row filling every required column the 033 schema has."""
    automation_id = uuid.uuid4().hex
    with sqlite3.connect(db_path) as connection:
        columns = connection.execute("PRAGMA table_info(automations)").fetchall()
        values: dict[str, object] = {}
        for _cid, name, column_type, not_null, default, _pk in columns:
            if name == "id":
                values[name] = automation_id
            elif not_null and default is None:
                values[name] = (
                    json.dumps({"type": "cron"})
                    if column_type == "JSON"
                    else 0
                    if column_type in ("BOOLEAN", "INTEGER")
                    else "x"
                )
        names = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO automations ({names}) VALUES ({placeholders})",  # noqa: S608 - column names come from PRAGMA
            list(values.values()),
        )
    return automation_id


def test_upgrade_backfills_empty_tags_and_downgrade_drops_them(tmp_path):
    db_path = str(tmp_path / "tags-migration.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    _alembic(db_url, "upgrade", "033")
    automation_id = _insert_pre_tags_automation(db_path)

    _alembic(db_url, "upgrade", "034")
    with sqlite3.connect(db_path) as connection:
        (tags,) = connection.execute(
            "SELECT tags FROM automations WHERE id = ?", (automation_id,)
        ).fetchone()
    assert json.loads(tags) == []

    _alembic(db_url, "downgrade", "033")
    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(automations)")
        }
    assert "tags" not in columns

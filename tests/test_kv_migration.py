"""Integration tests for the KV single-document -> per-key migration (028).

These run Alembic against a temporary SQLite database (no Docker) and exercise
the one-shot fan-out described in issue #523:

- a legacy aggregate document is decrypted into one row per key plus a metadata
  row carrying the original ``$version``;
- the legacy table is gone afterwards (no dual-read path);
- the downgrade reassembles a single document.

The "fresh install needs no ``AUTOMATION_KV_SECRET``" case is covered by
``tests/test_db.py::TestSqliteMigrations``, which upgrades a new SQLite database
to head without the secret.
"""

import os
import subprocess
import uuid

import pytest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "kv-migration-test-secret"


def _alembic(db_url: str, *args: str, secret: str | None = SECRET) -> None:
    env = os.environ.copy()
    env["AUTOMATION_DB_URL"] = db_url
    if secret is None:
        env.pop("AUTOMATION_KV_SECRET", None)
    else:
        env["AUTOMATION_KV_SECRET"] = secret
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, f"alembic {args} failed: {result.stderr}"


def _python(code: str) -> None:
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, f"helper failed: {result.stderr}"


@pytest.fixture
def sqlite_db_path(tmp_path):
    path = tmp_path / "kv-migration.db"
    yield str(path)
    if path.exists():
        path.unlink()


def _seed_legacy_document(db_path: str) -> str:
    """Insert one legacy aggregate row via the released 008 schema."""
    automation_id = str(uuid.uuid4())
    code = f"""
import sqlite3, sys
sys.path.insert(0, {PROJECT_ROOT!r})
from openhands.automation.utils.kv import encrypt_value
con = sqlite3.connect({db_path!r})
con.execute(
    "INSERT INTO automations (id, user_id, org_id, name, trigger, tarball_path,"
    " entrypoint, enabled) VALUES (?,?,?,?,?,?,?,1)",
    ({automation_id!r}, {str(uuid.uuid4())!r}, {str(uuid.uuid4())!r},
     "legacy", "{{}}", "s3://bucket/code.tar.gz", "run"),
)
state = {{"config": {{"host": "localhost"}}, "counter": 7, "$version": 3}}
con.execute(
    "INSERT INTO automation_kv (id, automation_id, state_encrypted)"
    " VALUES (?,?,?)",
    ({str(uuid.uuid4())!r}, {automation_id!r}, encrypt_value({SECRET!r}, state)),
)
con.commit()
"""
    _python(code)
    return automation_id


def test_legacy_document_fans_out(sqlite_db_path):
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", "027")
    _seed_legacy_document(sqlite_db_path)

    _alembic(db_url, "upgrade", "head")

    code = f"""
import sqlite3, sys
sys.path.insert(0, {PROJECT_ROOT!r})
from openhands.automation.utils.kv import decrypt_value
con = sqlite3.connect({sqlite_db_path!r})
tables = {{r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}}
assert "automation_kv_meta" in tables
assert "automation_kv_legacy" not in tables

rows = con.execute(
    "SELECT key, value_encrypted FROM automation_kv ORDER BY key").fetchall()
assert [r[0] for r in rows] == ["config", "counter"], rows
values = {{k: decrypt_value({SECRET!r}, v) for k, v in rows}}
assert values["config"] == {{"host": "localhost"}}
assert values["counter"] == 7

meta = con.execute("SELECT version FROM automation_kv_meta").fetchone()
assert meta == (3,), meta
print("OK")
"""
    _python(code)


def test_downgrade_reassembles_document(sqlite_db_path):
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", "027")
    _seed_legacy_document(sqlite_db_path)
    _alembic(db_url, "upgrade", "head")
    _alembic(db_url, "downgrade", "027")

    code = f"""
import sqlite3, sys
sys.path.insert(0, {PROJECT_ROOT!r})
from openhands.automation.utils.kv import decrypt_value
con = sqlite3.connect({sqlite_db_path!r})
tables = {{r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}}
assert "automation_kv_meta" not in tables
rows = con.execute("SELECT state_encrypted FROM automation_kv").fetchall()
assert len(rows) == 1, rows
state = decrypt_value({SECRET!r}, rows[0][0])
assert state == {{"config": {{"host": "localhost"}}, "counter": 7, "$version": 3}}
print("OK")
"""
    _python(code)

"""Integration tests for the KV single-document -> per-key migration (030).

These build the released main schema through revision 028 in a temporary SQLite
database, populate its encrypted aggregate KV table, and exercise the one-shot
fan-out described in issue #523:

- a legacy aggregate document is decrypted into one row per key plus a metadata
  row carrying the original ``$version``;
- the legacy table is gone afterwards (no dual-read path);
- the downgrade reassembles a single document;
- a failed upgrade from 028 or 029 leaves the schema, revision and ciphertext
  exactly as they were, and the partial states earlier releases left behind
  after such a failure are repaired on the next upgrade
  (OpenHands/OpenHands#18299).

The "fresh install needs no ``AUTOMATION_KV_SECRET``" case is covered by
``tests/test_db.py::TestSqliteMigrations``, which upgrades a new SQLite database
to head without the secret.
"""

import os
import sqlite3
import subprocess
import uuid
from contextlib import closing

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "kv-migration-test-secret"
WRONG_SECRET = "not-the-secret-the-state-was-written-with"

# What a failed upgrade left behind on SQLite before migrations ran in one
# transaction (OpenHands/OpenHands#18299), copied from what released 029/030
# create: 029's column on revision 028, or 030's rename and new, still empty
# tables on revision 029.
PARTIAL_029 = [
    "ALTER TABLE automation_git_sync_org_config ADD COLUMN"
    " consecutive_auth_failures INTEGER DEFAULT '0' NOT NULL",
]
PARTIAL_030 = [
    "ALTER TABLE automation_kv RENAME TO automation_kv_legacy",
    "CREATE TABLE automation_kv (id CHAR(32) NOT NULL,"
    ' automation_id CHAR(32) NOT NULL, "key" VARCHAR(255) NOT NULL,'
    " value_encrypted TEXT NOT NULL,"
    " created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,"
    " updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,"
    " PRIMARY KEY (id),"
    " FOREIGN KEY(automation_id) REFERENCES automations (id) ON DELETE CASCADE)",
    "CREATE UNIQUE INDEX ix_automation_kv_automation_id_key"
    ' ON automation_kv (automation_id, "key")',
    "CREATE TABLE automation_kv_meta (automation_id CHAR(32) NOT NULL,"
    " version BIGINT DEFAULT '0' NOT NULL,"
    " created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,"
    " updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,"
    " PRIMARY KEY (automation_id),"
    " FOREIGN KEY(automation_id) REFERENCES automations (id) ON DELETE CASCADE)",
]


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


def _alembic_expect_failure(db_url: str, *args: str, secret: str | None = None) -> str:
    """Run alembic expecting a non-zero exit; return combined output."""
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
    assert result.returncode != 0, f"alembic {args} unexpectedly succeeded"
    return result.stdout + result.stderr


@pytest.fixture
def sqlite_db_path(tmp_path):
    path = tmp_path / "kv-migration.db"
    yield str(path)
    if path.exists():
        path.unlink()


def _current_migration_head() -> str:
    config = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    assert head is not None
    return head


def _assert_revision(db_path: str, revision: str) -> None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (revision,)


def _snapshot(db_path: str) -> tuple[list, list, dict]:
    """Schema, revision and every KV row, to prove a failure changed nothing."""
    with closing(sqlite3.connect(db_path)) as connection:
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version")
        kv_rows = {
            name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
            for kind, name, _ in schema
            if kind == "table" and name.startswith("automation_kv")
        }
        return schema, revision.fetchall(), kv_rows


def _execute(db_path: str, statements: list[str]) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.commit()


def _table_names(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def _column_names(db_path: str, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in rows}


def _assert_pre_kv_migration_schema(db_path: str) -> None:
    _assert_revision(db_path, "028")
    tables = _table_names(db_path)
    assert "automation_kv_legacy" not in tables, tables
    assert "automation_kv_meta" not in tables, tables
    assert "automation_kv" in tables, tables
    assert "consecutive_auth_failures" not in _column_names(
        db_path, "automation_git_sync_org_config"
    )


def _seed_legacy_document(db_path: str) -> str:
    """Insert an aggregate KV row into the current main schema."""
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


def _assert_fanned_out(db_path: str) -> None:
    """The seeded legacy document is now per-key rows plus its ``$version``."""
    code = f"""
import sqlite3, sys
sys.path.insert(0, {PROJECT_ROOT!r})
from openhands.automation.utils.kv import decrypt_value
con = sqlite3.connect({db_path!r})
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


def test_legacy_document_fans_out(sqlite_db_path):
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", "028")
    _assert_revision(sqlite_db_path, "028")
    _seed_legacy_document(sqlite_db_path)

    _alembic(db_url, "upgrade", "head")

    _assert_revision(sqlite_db_path, _current_migration_head())
    _assert_fanned_out(sqlite_db_path)


def test_downgrade_reassembles_document(sqlite_db_path):
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", "028")
    _assert_revision(sqlite_db_path, "028")
    _seed_legacy_document(sqlite_db_path)
    _alembic(db_url, "upgrade", "head")
    _alembic(db_url, "downgrade", "028")

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


@pytest.mark.parametrize("start", ["028", "029"])
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        (None, "AUTOMATION_KV_SECRET is required"),
        (WRONG_SECRET, "AUTOMATION_KV_SECRET must be the secret"),
    ],
    ids=["missing-secret", "wrong-secret"],
)
def test_failed_upgrade_changes_nothing_and_can_be_retried(
    sqlite_db_path, start, secret, message
):
    """A KV upgrade that cannot decrypt the legacy state is rolled back whole.

    The legacy documents cannot be decrypted without the secret they were
    written with, so the upgrade must fail loudly rather than drop state. From
    028 the failure comes after 029 has already added its column; from 029,
    after 030 has renamed and created tables. Either way the schema, revision
    and ciphertext must be exactly what they were, so that a retry with the
    right secret completes (OpenHands/OpenHands#18299).
    """
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", start)
    _seed_legacy_document(sqlite_db_path)
    before = _snapshot(sqlite_db_path)

    output = _alembic_expect_failure(db_url, "upgrade", "head", secret=secret)
    assert message in output, output

    assert _snapshot(sqlite_db_path) == before
    _alembic(db_url, "upgrade", "head")
    _assert_revision(sqlite_db_path, _current_migration_head())
    _assert_fanned_out(sqlite_db_path)


@pytest.mark.parametrize(
    ("start", "partial"),
    [("028", PARTIAL_029), ("029", PARTIAL_030)],
    ids=["029-column-on-028", "030-tables-on-029"],
)
def test_upgrade_repairs_state_left_by_earlier_failure(sqlite_db_path, start, partial):
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", start)
    _seed_legacy_document(sqlite_db_path)
    _execute(sqlite_db_path, partial)

    _alembic(db_url, "upgrade", "head")

    _assert_revision(sqlite_db_path, _current_migration_head())
    _assert_fanned_out(sqlite_db_path)


def test_partial_030_state_with_rows_is_not_repaired(sqlite_db_path):
    """Only the empty tables a failed 030 leaves behind are dropped."""
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", "029")
    automation_id = _seed_legacy_document(sqlite_db_path)
    _execute(
        sqlite_db_path,
        [
            *PARTIAL_030,
            "INSERT INTO automation_kv_meta (automation_id, version)"
            f" VALUES ('{automation_id}', 1)",
        ],
    )
    before = _snapshot(sqlite_db_path)

    output = _alembic_expect_failure(db_url, "upgrade", "head", secret=SECRET)
    assert "nothing was changed" in output, output

    assert _snapshot(sqlite_db_path) == before


@pytest.mark.parametrize(
    ("secret", "expected_error"),
    [
        (None, "AUTOMATION_KV_SECRET is required"),
        ("wrong-kv-migration-test-secret", "AUTOMATION_KV_SECRET must be the secret"),
    ],
)
def test_legacy_rows_migration_failure_rolls_back_and_can_retry(
    sqlite_db_path, secret, expected_error
):
    """Failed 030 upgrades must not strand earlier SQLite DDL from 029."""
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", "028")
    _assert_revision(sqlite_db_path, "028")
    _seed_legacy_document(sqlite_db_path)

    output = _alembic_expect_failure(db_url, "upgrade", "head", secret=secret)
    assert expected_error in output, output

    _assert_pre_kv_migration_schema(sqlite_db_path)
    with sqlite3.connect(sqlite_db_path) as connection:
        rows = connection.execute(
            "SELECT state_encrypted FROM automation_kv"
        ).fetchall()
    assert len(rows) == 1, rows

    _alembic(db_url, "upgrade", "head")
    _assert_revision(sqlite_db_path, _current_migration_head())


def test_partial_029_sqlite_column_state_recovers(sqlite_db_path):
    db_url = f"sqlite:///{sqlite_db_path}"
    _alembic(db_url, "upgrade", "028")
    _seed_legacy_document(sqlite_db_path)
    with sqlite3.connect(sqlite_db_path) as connection:
        connection.execute(
            "ALTER TABLE automation_git_sync_org_config "
            "ADD COLUMN consecutive_auth_failures INTEGER DEFAULT '0' NOT NULL"
        )
        connection.commit()

    _assert_revision(sqlite_db_path, "028")
    assert "consecutive_auth_failures" in _column_names(
        sqlite_db_path, "automation_git_sync_org_config"
    )

    _alembic(db_url, "upgrade", "head")
    _assert_revision(sqlite_db_path, _current_migration_head())

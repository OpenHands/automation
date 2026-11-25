"""Migration 044: outcome facts gain effective_at, dedupe_key and dimensions."""

import os
import sqlite3
import subprocess
import uuid

from sqlalchemy import create_engine, inspect


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


def _insert_fact(db_path: str, created_at: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO automation_outcome_values (id, org_id, factory_id, source,"
            " key, value_type, numeric_value, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                "agent",
                "score",
                "number",
                3.0,
                created_at,
            ),
        )


def test_facts_written_before_the_upgrade_count_when_they_were_received(tmp_path):
    db_path = str(tmp_path / "outcomes.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "043")
    _insert_fact(db_path, "2026-11-20 09:30:00.000000")

    _alembic(db_url, "upgrade", "head")

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT created_at, effective_at, dedupe_key, dimensions"
            " FROM automation_outcome_values"
        ).fetchone()
    assert row == (
        "2026-11-20 09:30:00.000000",
        "2026-11-20 09:30:00.000000",
        None,
        None,
    )

    engine = create_engine(db_url)
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("automation_outcome_values")}
    indexes = {i["name"]: i for i in inspector.get_indexes("automation_outcome_values")}
    checks = {
        c["name"] for c in inspector.get_check_constraints("automation_outcome_values")
    }
    engine.dispose()
    assert columns["effective_at"]["nullable"] is False
    assert columns["dedupe_key"]["nullable"] is True
    assert indexes["ux_outcomes_dedupe_key"]["unique"]
    assert "ix_outcomes_factory_key_effective" in indexes
    assert {"outcome_key", "outcome_source", "outcome_typed_value"} <= checks


def test_the_upgrade_adds_one_head_and_downgrades_with_the_facts_kept(tmp_path):
    db_path = str(tmp_path / "heads.db")
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "head")
    assert _alembic(db_url, "heads").count("(head)") == 1
    _alembic(db_url, "downgrade", "043")
    _insert_fact(db_path, "2026-11-20 09:30:00.000000")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    names = {c["name"] for c in inspector.get_columns("automation_outcome_values")}
    indexes = {i["name"] for i in inspector.get_indexes("automation_outcome_values")}
    engine.dispose()
    assert names.isdisjoint({"effective_at", "dedupe_key", "dimensions"})
    assert {"ix_outcomes_factory_key_created", "ix_outcomes_run_id"} <= indexes
    assert "ux_outcomes_dedupe_key" not in indexes

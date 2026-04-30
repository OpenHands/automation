"""Tests for database module."""

import os
import tempfile
from pathlib import Path

import pytest

from automation.db import (
    _create_sqlite_engine,
    is_sqlite_url,
    set_sqlite_mode,
    using_sqlite,
)


# Get the project root directory (parent of tests/)
PROJECT_ROOT = Path(__file__).parent.parent


class TestIsSqliteUrl:
    """Tests for is_sqlite_url helper function."""

    def test_sqlite_url(self):
        """Standard SQLite URL is detected."""
        assert is_sqlite_url("sqlite:///test.db") is True

    def test_sqlite_aiosqlite_url(self):
        """SQLite with aiosqlite driver is detected."""
        assert is_sqlite_url("sqlite+aiosqlite:///test.db") is True

    def test_sqlite_absolute_path(self):
        """SQLite URL with absolute path is detected."""
        assert is_sqlite_url("sqlite+aiosqlite:////data/automations.db") is True

    def test_postgresql_url(self):
        """PostgreSQL URL is not detected as SQLite."""
        assert is_sqlite_url("postgresql://user:pass@host/db") is False

    def test_postgresql_asyncpg_url(self):
        """PostgreSQL with asyncpg driver is not detected as SQLite."""
        assert is_sqlite_url("postgresql+asyncpg://user:pass@host/db") is False

    def test_empty_url(self):
        """Empty URL is not detected as SQLite."""
        assert is_sqlite_url("") is False


class TestSqliteModeFlag:
    """Tests for SQLite mode flag functions."""

    def test_default_is_false(self):
        """Default mode is not SQLite."""
        set_sqlite_mode(False)  # Reset to default
        assert using_sqlite() is False

    def test_set_sqlite_mode_true(self):
        """Setting SQLite mode to True works."""
        set_sqlite_mode(True)
        assert using_sqlite() is True
        set_sqlite_mode(False)  # Reset

    def test_set_sqlite_mode_false(self):
        """Setting SQLite mode to False works."""
        set_sqlite_mode(True)
        set_sqlite_mode(False)
        assert using_sqlite() is False


class TestCreateSqliteEngine:
    """Tests for SQLite engine creation."""

    def test_creates_engine_with_aiosqlite_driver(self):
        """Engine is created with aiosqlite driver."""
        result = _create_sqlite_engine("sqlite:///test.db")
        assert result.is_sqlite is True
        assert result.connector is None
        # Check that the URL was converted to use aiosqlite
        url_str = str(result.engine.url)
        assert "aiosqlite" in url_str

    def test_preserves_aiosqlite_driver(self):
        """If aiosqlite is already specified, it's preserved."""
        result = _create_sqlite_engine("sqlite+aiosqlite:///test.db")
        assert result.is_sqlite is True
        url_str = str(result.engine.url)
        assert "aiosqlite" in url_str

    def test_absolute_path(self):
        """SQLite with absolute path works."""
        result = _create_sqlite_engine("sqlite+aiosqlite:////data/automations.db")
        assert result.is_sqlite is True


class TestEngineResult:
    """Tests for EngineResult dataclass."""

    def test_is_sqlite_default(self):
        """is_sqlite defaults to False."""
        from automation.db import EngineResult

        # Can't easily create a real engine without a database,
        # so just test the default value logic
        assert EngineResult.__dataclass_fields__["is_sqlite"].default is False

    @pytest.mark.asyncio
    async def test_dispose_without_connector(self):
        """Dispose works when connector is None."""
        result = _create_sqlite_engine("sqlite+aiosqlite:///:memory:")
        await result.dispose()  # Should not raise


class TestSqliteMigrations:
    """Tests for SQLite migration support."""

    def test_migrations_run_on_sqlite(self, monkeypatch):
        """Alembic migrations can run on SQLite via CLI."""
        import subprocess

        # Create a temporary SQLite database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            db_url = f"sqlite:///{db_path}"

            # Run alembic upgrade head via subprocess with the env var set
            # This ensures the env.py picks up AUTOMATION_DB_URL correctly
            env = os.environ.copy()
            env["AUTOMATION_DB_URL"] = db_url

            result = subprocess.run(
                ["uv", "run", "alembic", "upgrade", "head"],
                env=env,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
            assert result.returncode == 0, f"Alembic upgrade failed: {result.stderr}"

            # Verify tables were created by checking the schema
            from sqlalchemy import create_engine, inspect

            engine = create_engine(db_url)
            inspector = inspect(engine)
            tables = inspector.get_table_names()

            # Verify all expected tables exist
            assert "automations" in tables
            assert "automation_runs" in tables
            assert "tarball_uploads" in tables
            assert "custom_webhooks" in tables
            assert "alembic_version" in tables

            engine.dispose()
        finally:
            # Clean up
            if os.path.exists(db_path):
                os.unlink(db_path)

"""Tests for database module."""

import os
import tempfile
from pathlib import Path

import pytest

from openhands.automation.db import (
    _create_sqlite_engine,
    is_sqlite_url,
    normalize_sqlite_url_for_alembic,
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
        from openhands.automation.db import EngineResult

        # Can't easily create a real engine without a database,
        # so just test the default value logic
        assert EngineResult.__dataclass_fields__["is_sqlite"].default is False

    @pytest.mark.asyncio
    async def test_dispose_without_connector(self):
        """Dispose works when connector is None."""
        result = _create_sqlite_engine("sqlite+aiosqlite:///:memory:")
        await result.dispose()  # Should not raise


class TestNormalizeSqliteUrlForAlembic:
    """Tests for normalize_sqlite_url_for_alembic helper function."""

    def test_converts_aiosqlite_to_sqlite(self):
        """Converts sqlite+aiosqlite:// to sqlite://."""
        url = "sqlite+aiosqlite:///test.db"
        assert normalize_sqlite_url_for_alembic(url) == "sqlite:///test.db"

    def test_converts_aiosqlite_with_absolute_path(self):
        """Converts sqlite+aiosqlite with absolute path."""
        url = "sqlite+aiosqlite:////data/automations.db"
        assert normalize_sqlite_url_for_alembic(url) == "sqlite:////data/automations.db"

    def test_preserves_plain_sqlite_url(self):
        """Plain sqlite:// URL is unchanged."""
        url = "sqlite:///test.db"
        assert normalize_sqlite_url_for_alembic(url) == "sqlite:///test.db"

    def test_preserves_postgresql_url(self):
        """PostgreSQL URLs are unchanged."""
        url = "postgresql://user:pass@host/db"
        assert normalize_sqlite_url_for_alembic(url) == url

    def test_preserves_postgresql_asyncpg_url(self):
        """PostgreSQL+asyncpg URLs are unchanged."""
        url = "postgresql+asyncpg://user:pass@host/db"
        assert normalize_sqlite_url_for_alembic(url) == url

    def test_handles_memory_database(self):
        """Memory database URL is converted correctly."""
        url = "sqlite+aiosqlite:///:memory:"
        assert normalize_sqlite_url_for_alembic(url) == "sqlite:///:memory:"


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

            run_indexes = {
                index["name"] for index in inspector.get_indexes("automation_runs")
            }
            assert "ix_automation_runs_status_created_at" in run_indexes
            assert "ix_automation_runs_status_timeout_at" in run_indexes

            engine.dispose()
        finally:
            # Clean up
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_auto_migration_applies_schema(self):
        """Auto-migration on startup creates all required tables.

        This tests the auto-migration behavior added in app.py for SQLite,
        using the normalize_sqlite_url_for_alembic helper function.
        """
        import subprocess

        # Create a temporary SQLite database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Test the URL normalization helper
            async_url = f"sqlite+aiosqlite:///{db_path}"
            sync_url = normalize_sqlite_url_for_alembic(async_url)
            assert sync_url == f"sqlite:///{db_path}"

            # Run migrations using subprocess to avoid env.py PostgreSQL defaults
            env = os.environ.copy()
            env["AUTOMATION_DB_URL"] = sync_url

            result = subprocess.run(
                ["uv", "run", "alembic", "upgrade", "head"],
                env=env,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
            assert result.returncode == 0, f"Alembic upgrade failed: {result.stderr}"

            # Verify tables were created
            from sqlalchemy import create_engine, inspect

            engine = create_engine(sync_url)
            inspector = inspect(engine)
            tables = inspector.get_table_names()

            assert "automations" in tables
            assert "automation_runs" in tables
            assert "tarball_uploads" in tables
            assert "alembic_version" in tables

            engine.dispose()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_auto_migration_error_handling(self):
        """Migration failure returns non-zero exit code.

        This tests that migration failures are properly propagated.
        """
        import subprocess

        # Use an invalid database path that will cause a migration error
        env = os.environ.copy()
        # Path that doesn't exist and can't be created
        env["AUTOMATION_DB_URL"] = "sqlite:////nonexistent/path/test.db"

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        # The migration should fail (non-zero exit code)
        assert result.returncode != 0

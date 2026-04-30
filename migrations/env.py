"""Alembic migration environment.

Supports three database backends:

- **PostgreSQL** (default) via pg8000 (sync driver) — Alembic runs
  synchronously so pg8000 is used instead of the application's asyncpg.
- **GCP Cloud SQL** — Uses the Cloud SQL Python connector with pg8000.
- **SQLite** — For local / open-source deployments.  Enabled by setting
  ``AUTOMATION_DB_URL`` to a ``sqlite:///`` URL.

PostgreSQL migrations use advisory locks for safe concurrent execution.
SQLite skips advisory locks since it only supports single-writer access.
"""

import os

from alembic import context
from sqlalchemy import create_engine, text

from automation.models import Base


target_metadata = Base.metadata

# Advisory lock ID for migrations (arbitrary unique integer)
# Using a hash of "automation_migrations" to avoid collisions
MIGRATION_LOCK_ID = 849320147

# Full URL takes precedence when set (supports both PostgreSQL and SQLite)
DB_URL = os.getenv("AUTOMATION_DB_URL")

DB_USER = os.getenv("AUTOMATION_DB_USER", os.getenv("DB_USER", "postgres"))
DB_PASS = os.getenv("AUTOMATION_DB_PASS", os.getenv("DB_PASS", "postgres"))
DB_HOST = os.getenv("AUTOMATION_DB_HOST", os.getenv("DB_HOST", "localhost"))
DB_PORT = os.getenv("AUTOMATION_DB_PORT", os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("AUTOMATION_DB_NAME", os.getenv("DB_NAME", "automations"))

GCP_DB_INSTANCE = os.getenv("AUTOMATION_GCP_DB_INSTANCE", os.getenv("GCP_DB_INSTANCE"))
GCP_PROJECT = os.getenv("AUTOMATION_GCP_PROJECT", os.getenv("GCP_PROJECT"))
GCP_REGION = os.getenv("AUTOMATION_GCP_REGION", os.getenv("GCP_REGION"))


def _is_sqlite_url(url: str | None) -> bool:
    return url is not None and url.startswith("sqlite")


def get_engine(database_name=DB_NAME):
    # --- SQLite ---------------------------------------------------------
    if _is_sqlite_url(DB_URL):
        # Strip the async driver prefix so Alembic can use the sync pysqlite
        sync_url = DB_URL.replace("sqlite+aiosqlite", "sqlite")
        engine = create_engine(sync_url)

        # Enable foreign keys for every connection (off by default in SQLite)
        from sqlalchemy import event as sa_event

        @sa_event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    # --- GCP Cloud SQL --------------------------------------------------
    if GCP_DB_INSTANCE:
        from google.cloud.sql.connector import Connector

        def get_db_connection():
            connector = Connector()
            instance_string = f"{GCP_PROJECT}:{GCP_REGION}:{GCP_DB_INSTANCE}"
            return connector.connect(
                instance_string,
                "pg8000",
                user=DB_USER,
                password=DB_PASS.strip(),
                db=database_name,
            )

        return create_engine(
            "postgresql+pg8000://",
            creator=get_db_connection,
            pool_pre_ping=True,
        )

    # --- Direct PostgreSQL ----------------------------------------------
    if DB_URL and not _is_sqlite_url(DB_URL):
        # Caller provided a full PostgreSQL URL — normalise driver to pg8000
        sync_url = DB_URL.replace("postgresql+asyncpg", "postgresql+pg8000")
        return create_engine(sync_url, pool_pre_ping=True)

    url = f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database_name}"
    return create_engine(url, pool_pre_ping=True)


def run_migrations_offline():
    if _is_sqlite_url(DB_URL):
        sync_url = DB_URL.replace("sqlite+aiosqlite", "sqlite")
    else:
        sync_url = (
            f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        )
    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite_url(DB_URL),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations with advisory lock for safe concurrent execution.

    Uses PostgreSQL advisory locks to ensure only one migration process
    runs at a time, even when multiple pods/containers attempt migrations
    concurrently. Other processes will wait for the lock to be released.

    On SQLite, advisory locks are skipped (not supported / not needed).
    ``render_as_batch`` is enabled so ALTER TABLE operations work within
    SQLite's limited DDL capabilities.
    """
    engine = get_engine()
    is_sqlite = _is_sqlite_url(DB_URL)

    with engine.begin() as connection:
        if not is_sqlite:
            connection.execute(text(f"SELECT pg_advisory_lock({MIGRATION_LOCK_ID})"))
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=is_sqlite,
            )
            context.run_migrations()
        finally:
            if not is_sqlite:
                connection.execute(
                    text(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})")
                )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

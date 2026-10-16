"""Alembic migration environment.

Supports two database backends:
- PostgreSQL (pg8000): Default for cloud deployments
- SQLite: For local/self-hosted deployments

PostgreSQL mode:
- Uses GCP Cloud SQL connector for production
- Uses pg8000 driver (sync) for Alembic migrations
- Uses advisory locks for safe concurrent execution

SQLite mode:
- Reads AUTOMATION_DB_URL environment variable
- No advisory locks (single-process mode assumed)

Note: Uses pg8000 (sync driver) while the application uses asyncpg (async driver).
This is intentional - Alembic runs synchronously, and both drivers produce
identical DDL/schema operations. An AUTOMATION_DB_URL naming an async driver is
rewritten to its sync equivalent by alembic_engine_args(), which also carries the
URL's own ssl parameter over to pg8000.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from alembic import context
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.pool import NullPool

from openhands.automation.db import (
    _build_pg8000_connect_args,
    alembic_engine_args,
    normalize_url_for_alembic,
)
from openhands.automation.models import Base


target_metadata = Base.metadata

# Advisory lock ID for migrations (arbitrary unique integer)
# Using a hash of "automation_migrations" to avoid collisions
MIGRATION_LOCK_ID = 849320147

# SQLite URL (takes precedence if set)
DB_URL = os.getenv("AUTOMATION_DB_URL", "")

# PostgreSQL settings
DB_USER = os.getenv("AUTOMATION_DB_USER", os.getenv("DB_USER", "postgres"))
DB_PASS = os.getenv("AUTOMATION_DB_PASS", os.getenv("DB_PASS", "postgres"))
DB_HOST = os.getenv("AUTOMATION_DB_HOST", os.getenv("DB_HOST", "localhost"))
DB_PORT = os.getenv("AUTOMATION_DB_PORT", os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("AUTOMATION_DB_NAME", os.getenv("DB_NAME", "automations"))
DB_SSL_MODE = os.getenv(
    "AUTOMATION_DB_SSL_MODE", os.getenv("DB_SSL_MODE", os.getenv("PGSSLMODE", ""))
)

GCP_DB_INSTANCE = os.getenv("AUTOMATION_GCP_DB_INSTANCE", os.getenv("GCP_DB_INSTANCE"))
GCP_PROJECT = os.getenv("AUTOMATION_GCP_PROJECT", os.getenv("GCP_PROJECT"))
GCP_REGION = os.getenv("AUTOMATION_GCP_REGION", os.getenv("GCP_REGION"))


def is_sqlite() -> bool:
    """Check if we're using SQLite based on DB_URL."""
    return DB_URL.startswith("sqlite")


def _configure_sqlite_transaction_control(engine):
    @event.listens_for(engine, "connect")
    def _disable_sqlite_legacy_transactions(dbapi_connection, _connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_sqlite_transaction(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


@contextmanager
def migration_engine() -> Iterator[Engine]:
    """Yield an engine for one migration run, then close everything it opened.

    The service can run migrations inside its own process on startup, so
    nothing may outlive the run.

    Priority:
    1. AUTOMATION_DB_URL (supports SQLite and PostgreSQL URLs)
    2. GCP Cloud SQL connector
    3. Direct PostgreSQL connection
    """
    connector = None
    if DB_URL:
        # SQLite or explicit PostgreSQL URL
        url, connect_args = alembic_engine_args(DB_URL)
        engine = create_engine(url, connect_args=connect_args, poolclass=NullPool)
        if is_sqlite():
            _configure_sqlite_transaction_control(engine)
    elif GCP_DB_INSTANCE:
        from google.cloud.sql.connector import Connector

        connector = Connector()
        instance_string = f"{GCP_PROJECT}:{GCP_REGION}:{GCP_DB_INSTANCE}"

        def get_db_connection():
            return connector.connect(
                instance_string,
                "pg8000",
                user=DB_USER,
                password=DB_PASS.strip(),
                db=DB_NAME,
            )

        engine = create_engine(
            "postgresql+pg8000://", creator=get_db_connection, poolclass=NullPool
        )
    else:
        url = f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        engine = create_engine(
            url,
            connect_args=_build_pg8000_connect_args(DB_SSL_MODE),
            poolclass=NullPool,
        )
    try:
        yield engine
    finally:
        engine.dispose()
        if connector is not None:
            connector.close()


def run_migrations_offline():
    if DB_URL:
        url = normalize_url_for_alembic(DB_URL)
    else:
        url = f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Enable batch mode for SQLite to handle ALTER TABLE limitations
        render_as_batch=is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations with appropriate locking for the database backend.

    PostgreSQL: Uses advisory locks to ensure only one migration process
    runs at a time, even when multiple pods/containers attempt migrations
    concurrently.

    SQLite: No locking needed (single-process mode assumed).
    """
    use_sqlite = is_sqlite()

    with migration_engine() as engine, engine.begin() as connection:
        # Acquire advisory lock for PostgreSQL only. A transaction-level lock
        # is released only once the commit is visible, so a replica waiting
        # on it finds the database at head instead of re-running migrations.
        if not use_sqlite:
            lock_sql = f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})"
            connection.execute(text(lock_sql))

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Enable batch mode for SQLite to handle ALTER TABLE limitations
            render_as_batch=use_sqlite,
        )
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

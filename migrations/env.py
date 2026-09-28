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
identical DDL/schema operations.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from alembic import context
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.pool import NullPool

from openhands.automation.db import _build_pg8000_connect_args
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

# Create the PostgreSQL database before migrating if it does not exist. The
# database user needs the CREATEDB privilege.
CREATE_DATABASE_IF_MISSING = os.getenv(
    "AUTOMATION_CREATE_DATABASE_IF_MISSING", "false"
).lower() in ("true", "1")
# CREATE DATABASE runs from here. Every PostgreSQL server has this database.
MAINTENANCE_DB_NAME = "postgres"
# Advisory lock ID for creating the database: crc32 of "automation_create_database"
CREATE_DATABASE_LOCK_ID = 748434676

logger = logging.getLogger("alembic.env")


def is_sqlite() -> bool:
    """Check if we're using SQLite based on DB_URL."""
    return DB_URL.startswith("sqlite")


@contextmanager
def migration_engine(database: str | None = None) -> Iterator[Engine]:
    """Yield an engine for one migration run, then close everything it opened.

    The service can run migrations inside its own process on startup, so
    nothing may outlive the run. ``database`` overrides the configured
    PostgreSQL database name.

    Priority:
    1. AUTOMATION_DB_URL (supports SQLite and PostgreSQL URLs)
    2. GCP Cloud SQL connector
    3. Direct PostgreSQL connection
    """
    connector = None
    if DB_URL:
        # SQLite or explicit PostgreSQL URL
        url = DB_URL
        # For SQLite, remove async driver prefix if present (Alembic is sync)
        if url.startswith("sqlite+aiosqlite"):
            url = url.replace("sqlite+aiosqlite", "sqlite", 1)
        if database is not None:
            url = make_url(url).set(database=database)
        engine = create_engine(url, poolclass=NullPool)
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
                db=database or DB_NAME,
            )

        engine = create_engine(
            "postgresql+pg8000://", creator=get_db_connection, poolclass=NullPool
        )
    else:
        name = database or DB_NAME
        url = f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{name}"
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


def create_database_if_missing() -> None:
    """Create the configured PostgreSQL database if it does not exist yet."""
    name = make_url(DB_URL).database if DB_URL else DB_NAME
    if not name:
        raise RuntimeError("AUTOMATION_DB_URL does not name a database to create")
    with (
        migration_engine(MAINTENANCE_DB_NAME) as engine,
        engine.connect() as connection,
    ):
        # CREATE DATABASE cannot run inside a transaction.
        connection.execution_options(isolation_level="AUTOCOMMIT")
        # Replicas that start together take turns, so only the first one
        # creates the database.
        connection.execute(text(f"SELECT pg_advisory_lock({CREATE_DATABASE_LOCK_ID})"))
        exists = connection.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
        ).scalar()
        if not exists:
            logger.info("Creating database %s", name)
            quoted = connection.dialect.identifier_preparer.quote(name)
            connection.exec_driver_sql(f"CREATE DATABASE {quoted}")


def run_migrations_offline():
    if DB_URL:
        url = DB_URL
        if url.startswith("sqlite+aiosqlite"):
            url = url.replace("sqlite+aiosqlite", "sqlite", 1)
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
    if CREATE_DATABASE_IF_MISSING and not use_sqlite:
        create_database_if_missing()

    with migration_engine() as engine, engine.begin() as connection:
        # Acquire advisory lock for PostgreSQL only
        if not use_sqlite:
            connection.execute(text(f"SELECT pg_advisory_lock({MIGRATION_LOCK_ID})"))

        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # Enable batch mode for SQLite to handle ALTER TABLE limitations
                render_as_batch=use_sqlite,
            )
            context.run_migrations()
        finally:
            # Release the lock for PostgreSQL
            if not use_sqlite:
                unlock_sql = f"SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})"
                connection.execute(text(unlock_sql))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

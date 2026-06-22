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

import os

from alembic import context
from sqlalchemy import create_engine, text

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


def is_sqlite() -> bool:
    """Check if we're using SQLite based on DB_URL."""
    return DB_URL.startswith("sqlite")


def get_engine(database_name=DB_NAME):
    """Create database engine based on configuration.

    Priority:
    1. AUTOMATION_DB_URL (supports SQLite and PostgreSQL URLs)
    2. GCP Cloud SQL connector
    3. Direct PostgreSQL connection
    """
    # SQLite or explicit PostgreSQL URL
    if DB_URL:
        url = DB_URL
        # For SQLite, remove async driver prefix if present (Alembic is sync)
        if url.startswith("sqlite+aiosqlite"):
            url = url.replace("sqlite+aiosqlite", "sqlite", 1)
        return create_engine(url, pool_pre_ping=True)

    # GCP Cloud SQL
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

    # Direct PostgreSQL
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database_name}"
    return create_engine(
        url,
        connect_args=_build_pg8000_connect_args(DB_SSL_MODE),
        pool_pre_ping=True,
    )


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
    engine = get_engine()
    use_sqlite = is_sqlite()

    with engine.begin() as connection:
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

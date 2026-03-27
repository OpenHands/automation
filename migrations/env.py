"""Alembic migration environment.

Follows the same patterns as the OpenHands enterprise migrations:
- Reads DB connection from environment variables
- Supports GCP Cloud SQL connector for production
- Supports local PostgreSQL for development
- Uses PostgreSQL advisory locks for safe concurrent execution

Note: Uses pg8000 (sync driver) while the application uses asyncpg (async driver).
This is intentional - Alembic runs synchronously, and both drivers produce
identical DDL/schema operations. The GCP Cloud SQL connector's sync connect()
method pairs naturally with pg8000.
"""

import os

from alembic import context
from sqlalchemy import create_engine, text

from automation.models import Base


target_metadata = Base.metadata

# Advisory lock ID for migrations (arbitrary unique integer)
# Using a hash of "automation_migrations" to avoid collisions
MIGRATION_LOCK_ID = 849320147

DB_USER = os.getenv("AUTOMATION_DB_USER", os.getenv("DB_USER", "postgres"))
DB_PASS = os.getenv("AUTOMATION_DB_PASS", os.getenv("DB_PASS", "postgres"))
DB_HOST = os.getenv("AUTOMATION_DB_HOST", os.getenv("DB_HOST", "localhost"))
DB_PORT = os.getenv("AUTOMATION_DB_PORT", os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("AUTOMATION_DB_NAME", os.getenv("DB_NAME", "automations"))

GCP_DB_INSTANCE = os.getenv("AUTOMATION_GCP_DB_INSTANCE", os.getenv("GCP_DB_INSTANCE"))
GCP_PROJECT = os.getenv("AUTOMATION_GCP_PROJECT", os.getenv("GCP_PROJECT"))
GCP_REGION = os.getenv("AUTOMATION_GCP_REGION", os.getenv("GCP_REGION"))


def get_engine(database_name=DB_NAME):
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
    else:
        url = f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database_name}"
        return create_engine(url, pool_pre_ping=True)


def run_migrations_offline():
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations with advisory lock for safe concurrent execution.

    Uses PostgreSQL advisory locks to ensure only one migration process
    runs at a time, even when multiple pods/containers attempt migrations
    concurrently. Other processes will wait for the lock to be released.
    """
    engine = get_engine()
    # Use engine.begin() for auto-commit behavior (required in SQLAlchemy 2.0)
    with engine.begin() as connection:
        # Acquire advisory lock - blocks until lock is available
        # This ensures only one migration runs at a time across all pods
        connection.execute(text(f"SELECT pg_advisory_lock({MIGRATION_LOCK_ID})"))
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            # Release the lock so other waiting processes can proceed
            connection.execute(text(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})"))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

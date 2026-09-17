"""Upgrade both schemas that were deployed with the same revision 022."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus


@pytest.mark.parametrize("schema", ["legacy_022", "current_022", "fresh"])
def test_subject_release_upgrade_preserves_runs(schema, tmp_path, monkeypatch):
    root = Path(__file__).parent.parent
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    monkeypatch.setenv("AUTOMATION_DB_URL", url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    engine = sa.create_engine(url)
    try:
        target = "021" if schema == "legacy_022" else "022"
        command.upgrade(config, "head" if schema == "fresh" else target)
        if schema == "legacy_022":
            # Exact 022 DDL from c756d24c91bc12da8bb329be815824bbc77dafe5:
            # the original index had a PostgreSQL predicate, but no SQLite one.
            with engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                operations.add_column(
                    "automation_runs", sa.Column("subject_key", sa.String(500))
                )
                operations.create_index(
                    "ix_automation_runs_subject",
                    "automation_runs",
                    ["automation_id", "subject_key", "created_at"],
                    postgresql_where=sa.text("subject_key IS NOT NULL"),
                )
            command.stamp(config, "022")
            # This is the deployed failure state: later migrations applied,
            # but Alembic could not see that revision 022's schema was stale.
            command.upgrade(config, "025")
            with Session(engine) as session:
                with pytest.raises(OperationalError, match="subject_released_at"):
                    session.scalars(sa.select(AutomationRun)).all()

        automation_id, run_id = uuid4(), uuid4()
        released_at = None if schema == "legacy_022" else datetime(2026, 9, 13)
        with engine.begin() as connection:
            connection.execute(
                sa.insert(Automation).values(
                    id=automation_id,
                    user_id=uuid4(),
                    org_id=uuid4(),
                    name="existing automation",
                    trigger={},
                    tarball_path="fixture.tar.gz",
                    entrypoint="python main.py",
                )
            )
            values = {"subject_released_at": released_at} if released_at else {}
            connection.execute(
                sa.insert(AutomationRun).values(
                    id=run_id,
                    automation_id=automation_id,
                    status=AutomationRunStatus.COMPLETED,
                    subject_key="repo/issue/1",
                    **values,
                )
            )

        command.upgrade(config, "head")
        command.upgrade(config, "head")  # Normal subsequent service startup.
        with Session(engine) as session:
            run = session.scalars(sa.select(AutomationRun)).one()
            assert run.id == run_id
            assert run.subject_key == "repo/issue/1"
            assert run.status == AutomationRunStatus.COMPLETED
            assert run.subject_released_at == released_at
        index = next(
            item
            for item in sa.inspect(engine).get_indexes("automation_runs")
            if item["name"] == "ix_automation_runs_subject"
        )
        assert str(index.get("dialect_options", {}).get("sqlite_where")) == (
            "subject_key IS NOT NULL AND subject_released_at IS NULL"
        )
        command.downgrade(config, "022")
        # Current ORM models include fields from later migrations, so inspect
        # revision 022 with SQL instead of trying to load it through the model.
        with engine.connect() as connection:
            downgraded = connection.execute(
                sa.text("SELECT subject_released_at FROM automation_runs")
            ).scalar_one()
        assert (downgraded is None) == (released_at is None)
        command.upgrade(config, "head")
        with Session(engine) as session:
            run = session.get(AutomationRun, run_id)
            assert run is not None
            assert run.subject_released_at == released_at
    finally:
        engine.dispose()

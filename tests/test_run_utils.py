"""Unit tests for automation run helpers."""

import uuid

import pytest

from openhands.automation.models import Automation, AutomationRun
from openhands.automation.utils.run import create_pending_run


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.executed: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)


@pytest.mark.asyncio
async def test_create_pending_run_stores_observability_parent_context():
    automation = Automation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Manual test",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="oh-internal://uploads/test.tar.gz",
        entrypoint="python main.py",
    )
    session = FakeSession()

    run = await create_pending_run(
        session,  # type: ignore[arg-type]
        automation,
        trigger_source="manual",
        observability_parent_span_context="serialized-manual-dispatch-context",
    )

    assert isinstance(run, AutomationRun)
    assert session.added == [run]
    assert session.executed
    assert run.trigger_source == "manual"
    assert run.observability_parent_span_context == "serialized-manual-dispatch-context"

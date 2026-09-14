"""Lifecycle state at the Git import boundary.

The new state field must work without the deprecated enabled field. When both
are explicit, imports must reject contradictions just as API requests do.
These tests use the real YAML decoder, importer, database, and scheduler query.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from openhands.automation.db import set_sqlite_mode, using_sqlite
from openhands.automation.git_sync.loop import _create_automation_from_git, _Owner
from openhands.automation.git_sync.serializer import deserialize_automation
from openhands.automation.models import Automation, AutomationState, Base
from openhands.automation.scheduler import _fetch_enabled_automations
from openhands.automation.utils.time import utcnow


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
async def state_session() -> AsyncIterator[AsyncSession]:
    previous_sqlite_mode = using_sqlite()
    set_sqlite_mode(True)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()
        set_sqlite_mode(previous_sqlite_mode)


async def _import_automation(
    session: AsyncSession, lifecycle_fields: dict[str, Any]
) -> Automation:
    fields = {
        "name": "Git state regression",
        "entrypoint": "python main.py",
        "trigger": {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        "tarball_source": {
            "type": "external",
            "url": "https://example.com/automation.tar.gz",
        },
        **lifecycle_fields,
    }
    files = {"automation.yaml": yaml.safe_dump(fields).encode()}
    deserialized = deserialize_automation(files)
    assert deserialized is not None
    # Match the import loop's savepoint: rejected input cannot leave partial rows.
    async with session.begin_nested():
        await _create_automation_from_git(
            session,
            _Owner(TEST_USER_ID, TEST_ORG_ID),
            "git-state-regression",
            deserialized,
            files,
            "test-head",
            [],
        )
    await session.flush()
    automation = await session.scalar(select(Automation))
    assert automation is not None
    return automation


async def test_inactive_state_without_enabled_is_not_scheduled(state_session):
    automation = await _import_automation(state_session, {"state": "INACTIVE"})
    scheduled = await _fetch_enabled_automations(
        state_session, batch_size=100, poll_threshold=utcnow()
    )

    assert (automation.state, automation.enabled, len(scheduled)) == (
        AutomationState.INACTIVE,
        False,
        0,
    ), "An explicitly INACTIVE Git definition must not become a scheduled automation"


@pytest.mark.parametrize(
    "lifecycle_fields",
    [
        pytest.param({"state": "INACTIVE", "enabled": True}, id="inactive-enabled"),
        pytest.param({"state": "ACTIVE", "enabled": False}, id="active-disabled"),
    ],
)
async def test_import_rejects_conflicting_state_and_enabled(
    state_session, lifecycle_fields
):
    with pytest.raises(ValueError, match="enabled.*state|state.*enabled"):
        await _import_automation(state_session, lifecycle_fields)
    assert await state_session.scalar(select(Automation.id)) is None


@pytest.mark.parametrize(
    ("lifecycle_fields", "expected_state", "expected_enabled"),
    [
        pytest.param({}, AutomationState.ACTIVE, True, id="legacy-default"),
        pytest.param({"enabled": True}, AutomationState.ACTIVE, True, id="legacy-on"),
        pytest.param(
            {"enabled": False}, AutomationState.INACTIVE, False, id="legacy-off"
        ),
        pytest.param(
            {"enabled": None}, AutomationState.ACTIVE, True, id="legacy-empty"
        ),
        pytest.param(
            {"state": "ACTIVE", "enabled": True},
            AutomationState.ACTIVE,
            True,
            id="consistent-active",
        ),
        pytest.param(
            {"state": "INACTIVE", "enabled": False},
            AutomationState.INACTIVE,
            False,
            id="consistent-inactive",
        ),
        pytest.param(
            {"state": "DRAFT", "enabled": False},
            AutomationState.DRAFT,
            False,
            id="consistent-draft",
        ),
    ],
)
async def test_import_preserves_legacy_and_consistent_state_inputs(
    state_session, lifecycle_fields, expected_state, expected_enabled
):
    automation = await _import_automation(state_session, lifecycle_fields)
    assert automation.state == expected_state
    assert automation.enabled is expected_enabled

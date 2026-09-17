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
from openhands.automation.git_sync.loop import (
    _create_automation_from_git,
    _Owner,
    _update_automation_from_git,
)
from openhands.automation.git_sync.serializer import deserialize_automation
from openhands.automation.models import (
    Automation,
    AutomationGitSyncState,
    AutomationState,
    Base,
    TarballUpload,
)
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
        pytest.param(
            {"state": "ACTIVE", "enabled": "false"},
            id="active-string-disabled",
        ),
    ],
)
async def test_import_rejects_conflicting_state_and_enabled(
    state_session, lifecycle_fields
):
    with pytest.raises(ValueError, match="enabled.*state|state.*enabled"):
        await _import_automation(state_session, lifecycle_fields)
    assert await state_session.scalar(select(Automation.id)) is None


async def test_invalid_state_metadata_rejects_before_tarball_upload(
    state_session, monkeypatch
):
    upload_attempted = False

    async def fail_if_upload_attempted(*args, **kwargs):
        nonlocal upload_attempted
        upload_attempted = True
        raise AssertionError("tarball upload should not run before state validation")

    monkeypatch.setattr(
        "openhands.automation.git_sync.loop._write_tarball_upload",
        fail_if_upload_attempted,
    )
    fields = {
        "name": "Git state regression",
        "entrypoint": "python main.py",
        "trigger": {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        "state": "ACTIVE",
        "enabled": False,
    }
    files = {
        "automation.yaml": yaml.safe_dump(fields).encode(),
        "tarball/main.py": b"print('hello')\n",
    }
    deserialized = deserialize_automation(files)
    assert deserialized is not None
    assert deserialized.tarball_bytes is not None

    with pytest.raises(ValueError, match="enabled must be true"):
        async with state_session.begin_nested():
            await _create_automation_from_git(
                state_session,
                _Owner(TEST_USER_ID, TEST_ORG_ID),
                "git-state-regression",
                deserialized,
                files,
                "test-head",
                [],
            )

    assert upload_attempted is False
    assert await state_session.scalar(select(Automation.id)) is None
    assert await state_session.scalar(select(TarballUpload.id)) is None


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
            {"state": "INACTIVE", "enabled": "false"},
            AutomationState.INACTIVE,
            False,
            id="consistent-inactive-string",
        ),
        pytest.param(
            {"state": "DRAFT", "enabled": False},
            AutomationState.DRAFT,
            False,
            id="consistent-draft",
        ),
        pytest.param(
            {"state": "DRAFT", "enabled": "false"},
            AutomationState.DRAFT,
            False,
            id="consistent-draft-string",
        ),
    ],
)
async def test_import_preserves_legacy_and_consistent_state_inputs(
    state_session, lifecycle_fields, expected_state, expected_enabled
):
    automation = await _import_automation(state_session, lifecycle_fields)
    assert automation.state == expected_state
    assert automation.enabled is expected_enabled


async def test_import_update_rejects_moving_existing_automation_to_draft(
    state_session,
):
    automation = await _import_automation(state_session, {})
    state = await state_session.scalar(
        select(AutomationGitSyncState).where(
            AutomationGitSyncState.automation_id == automation.id
        )
    )
    assert state is not None

    fields = {
        "name": "Git state regression",
        "entrypoint": "python main.py",
        "trigger": {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        "tarball_source": {
            "type": "external",
            "url": "https://example.com/automation.tar.gz",
        },
        "state": "DRAFT",
        "enabled": False,
    }
    files = {"automation.yaml": yaml.safe_dump(fields).encode()}
    deserialized = deserialize_automation(files)
    assert deserialized is not None

    with pytest.raises(ValueError, match="cannot be moved to draft"):
        await _update_automation_from_git(
            state_session,
            state,
            deserialized,
            files,
            "new-head",
            [],
        )

    await state_session.refresh(automation)
    assert automation.state == AutomationState.ACTIVE
    assert automation.enabled is True

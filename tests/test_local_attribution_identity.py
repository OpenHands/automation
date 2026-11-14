"""Conversation attribution identity forwarded to runs (#675).

Every local installation owns its rows as the same deterministic placeholder
user, so forwarding ``automation.user_id`` as ``AUTOMATION_USER_ID`` (which the
preset runners pass as the conversation ``user_id``, and the agent-server uses
as the analytics distinct id) collapsed all local installations into one
analytics person.
"""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from openhands.automation.auth import _get_local_user
from openhands.automation.config import clear_config_cache
from openhands.automation.dispatcher import _execute_run
from openhands.automation.execution import DispatchResult
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Base,
)


LOCAL_PLACEHOLDER_USER_ID = str(_get_local_user().user_id)


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "dummy-agent-key")
    clear_config_cache()
    yield
    clear_config_cache()


async def _open_installation(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )


async def _dispatch_and_capture_env(
    session_factory: async_sessionmaker[AsyncSession],
    mock_settings,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> dict[str, str]:
    """Dispatch one run through the real ``_execute_run`` and return its env."""
    async with session_factory() as session:
        automation = Automation(
            user_id=user_id,
            org_id=org_id,
            name="attribution",
            trigger={"type": "cron", "schedule": "0 0 1 1 *", "timezone": "UTC"},
            tarball_path="https://example.com/code.tar.gz",
            entrypoint="python3 main.py",
            enabled=True,
        )
        session.add(automation)
        await session.flush()
        run = AutomationRun(
            automation_id=automation.id, status=AutomationRunStatus.RUNNING
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    async with session_factory() as session:
        run = (
            await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.id == run_id)
            )
        ).scalar_one()

    with patch(
        "openhands.automation.dispatcher.execute_in_context",
        new_callable=AsyncMock,
        return_value=DispatchResult(success=True, sandbox_id=None, error=None),
    ) as execute:
        await _execute_run(run, mock_settings, session_factory, MagicMock())
    execute.assert_awaited_once()
    assert execute.await_args is not None
    return execute.await_args.kwargs["env_vars"]


async def test_local_installations_get_distinct_stable_attribution_ids(
    tmp_path, local_mode, mock_settings
):
    local_user = _get_local_user()
    kwargs = {"user_id": local_user.user_id, "org_id": local_user.org_id}

    engine_a, installation_a = await _open_installation(tmp_path / "a.db")
    engine_b, installation_b = await _open_installation(tmp_path / "b.db")
    try:
        env_a = await _dispatch_and_capture_env(installation_a, mock_settings, **kwargs)
        env_b = await _dispatch_and_capture_env(installation_b, mock_settings, **kwargs)
    finally:
        await engine_a.dispose()
        await engine_b.dispose()

    # A restart of installation A: a new engine on the same database.
    engine_a, installation_a = await _open_installation(tmp_path / "a.db")
    try:
        env_a_restarted = await _dispatch_and_capture_env(
            installation_a, mock_settings, **kwargs
        )
    finally:
        await engine_a.dispose()

    assert env_a["AUTOMATION_USER_ID"] != env_b["AUTOMATION_USER_ID"]
    assert LOCAL_PLACEHOLDER_USER_ID not in {
        env_a["AUTOMATION_USER_ID"],
        env_b["AUTOMATION_USER_ID"],
    }
    assert env_a_restarted["AUTOMATION_USER_ID"] == env_a["AUTOMATION_USER_ID"]
    # Ownership is not part of the change: the run still belongs to the owner.
    assert env_a["AUTOMATION_ORG_ID"] == str(local_user.org_id)


async def test_local_run_omits_attribution_when_installation_id_unavailable(
    tmp_path, local_mode, mock_settings
):
    local_user = _get_local_user()
    engine, installation = await _open_installation(tmp_path / "a.db")
    try:
        with patch(
            "openhands.automation.dispatcher.get_automation_backend_distinct_id",
            new_callable=AsyncMock,
            return_value=None,
        ):
            env = await _dispatch_and_capture_env(
                installation,
                mock_settings,
                user_id=local_user.user_id,
                org_id=local_user.org_id,
            )
    finally:
        await engine.dispose()

    assert "AUTOMATION_USER_ID" not in env


async def test_cloud_run_keeps_owner_user_id(tmp_path, mock_settings):
    clear_config_cache()
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    backend = MagicMock(is_local_mode=False)
    backend.get_execution_context = AsyncMock()
    backend.build_env_vars = MagicMock(return_value={})
    backend.release_context = AsyncMock()
    engine, installation = await _open_installation(tmp_path / "cloud.db")
    try:
        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            env = await _dispatch_and_capture_env(
                installation, mock_settings, user_id=user_id, org_id=org_id
            )
    finally:
        await engine.dispose()

    assert env["AUTOMATION_USER_ID"] == str(user_id)

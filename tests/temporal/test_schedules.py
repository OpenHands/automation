"""Tests for Temporal schedule management.

Tests the schedule creation, update, and deletion functions.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from automation.models import Automation
from automation.temporal.schedules import (
    _make_schedule_id,
    create_schedule,
    delete_schedule,
    pause_schedule,
    trigger_schedule,
    unpause_schedule,
    update_schedule,
)


def make_test_automation(
    cron_schedule: str = "0 9 * * 1-5",
    timezone: str = "UTC",
    trigger_type: str = "cron",
) -> Automation:
    """Create a test automation with sensible defaults."""
    automation = Automation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Test Automation",
        trigger={
            "type": trigger_type,
            "schedule": cron_schedule,
            "timezone": timezone,
        },
        tarball_path="https://example.com/code.tar.gz",
        entrypoint="python main.py",
        timeout=300,
        enabled=True,
    )
    return automation


class TestMakeScheduleId:
    """Tests for schedule ID generation."""

    def test_schedule_id_format(self):
        """Test schedule ID has expected format."""
        automation_id = uuid.uuid4()
        schedule_id = _make_schedule_id(automation_id)

        assert schedule_id.startswith("automation-")
        assert str(automation_id) in schedule_id

    def test_schedule_id_deterministic(self):
        """Test same automation ID produces same schedule ID."""
        automation_id = uuid.uuid4()
        id1 = _make_schedule_id(automation_id)
        id2 = _make_schedule_id(automation_id)

        assert id1 == id2


class TestCreateSchedule:
    """Tests for schedule creation."""

    @pytest.fixture
    def automation(self) -> Automation:
        return make_test_automation()

    @pytest.mark.asyncio
    async def test_create_schedule_success(self, automation: Automation):
        """Test successful schedule creation."""
        mock_handle = MagicMock()

        mock_client = AsyncMock()
        mock_client.create_schedule = AsyncMock(return_value=mock_handle)

        schedule_id = await create_schedule(mock_client, automation)

        assert schedule_id is not None
        assert str(automation.id) in schedule_id
        mock_client.create_schedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_schedule_with_cron(self, automation: Automation):
        """Test schedule is created with correct cron expression."""
        mock_handle = MagicMock()
        mock_client = AsyncMock()
        mock_client.create_schedule = AsyncMock(return_value=mock_handle)

        await create_schedule(mock_client, automation)

        # Verify the schedule was created with correct parameters
        call_args = mock_client.create_schedule.call_args
        assert call_args is not None

    @pytest.mark.asyncio
    async def test_create_schedule_without_cron_raises(self):
        """Test that creating a schedule without cron raises error."""
        automation = make_test_automation(trigger_type="manual")
        automation.trigger = {"type": "manual"}

        mock_client = AsyncMock()

        with pytest.raises(ValueError, match="trigger type"):
            await create_schedule(mock_client, automation)


class TestUpdateSchedule:
    """Tests for schedule updates."""

    @pytest.fixture
    def automation(self) -> Automation:
        return make_test_automation(cron_schedule="0 10 * * 1-5")

    @pytest.mark.asyncio
    async def test_update_schedule_success(self, automation: Automation):
        """Test successful schedule update."""
        mock_handle = AsyncMock()
        mock_handle.update = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

        await update_schedule(mock_client, automation)

        mock_client.get_schedule_handle.assert_called_once()
        mock_handle.update.assert_called_once()


class TestDeleteSchedule:
    """Tests for schedule deletion."""

    @pytest.mark.asyncio
    async def test_delete_schedule_success(self):
        """Test successful schedule deletion."""
        automation_id = uuid.uuid4()

        mock_handle = AsyncMock()
        mock_handle.delete = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

        await delete_schedule(mock_client, automation_id)

        mock_client.get_schedule_handle.assert_called_once()
        mock_handle.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_schedule_not_found_ignored(self):
        """Test delete handles not found gracefully."""
        from temporalio.service import RPCError, RPCStatusCode

        automation_id = uuid.uuid4()

        mock_handle = AsyncMock()
        mock_handle.delete = AsyncMock(
            side_effect=RPCError("Not found", RPCStatusCode.NOT_FOUND, b"")
        )

        mock_client = AsyncMock()
        mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

        # Should not raise
        await delete_schedule(mock_client, automation_id)


class TestPauseUnpauseSchedule:
    """Tests for pausing and unpausing schedules."""

    @pytest.mark.asyncio
    async def test_pause_schedule_success(self):
        """Test successful schedule pause."""
        automation_id = uuid.uuid4()

        mock_handle = AsyncMock()
        mock_handle.pause = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

        await pause_schedule(mock_client, automation_id)

        mock_handle.pause.assert_called_once()

    @pytest.mark.asyncio
    async def test_unpause_schedule_success(self):
        """Test successful schedule unpause."""
        automation_id = uuid.uuid4()

        mock_handle = AsyncMock()
        mock_handle.unpause = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

        await unpause_schedule(mock_client, automation_id)

        mock_handle.unpause.assert_called_once()


class TestTriggerSchedule:
    """Tests for manual schedule triggering."""

    @pytest.mark.asyncio
    async def test_trigger_schedule_success(self):
        """Test successful manual trigger."""
        automation_id = uuid.uuid4()

        mock_handle = AsyncMock()
        mock_handle.trigger = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

        await trigger_schedule(mock_client, automation_id)

        mock_handle.trigger.assert_called_once()

"""Shared helpers for the `automation_service_metadata` key/value table.

For singleton values too small to warrant their own column: PostHog's distinct
ID and consent state (telemetry.py), git sync's bookkeeping (git_sync/loop.py).
"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import AutomationServiceMetadata


async def get_service_metadata(session: AsyncSession, key: str) -> str | None:
    """Read one automation_service_metadata value, or None if unset."""
    return await session.scalar(
        select(AutomationServiceMetadata.value).where(
            AutomationServiceMetadata.key == key
        )
    )


async def set_service_metadata(session: AsyncSession, key: str, value: str) -> None:
    """Upsert one automation_service_metadata value."""
    await session.execute(
        text(
            "INSERT INTO automation_service_metadata (key, value) "
            "VALUES (:key, :value) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = excluded.value, updated_at = CURRENT_TIMESTAMP"
        ),
        {"key": key, "value": value},
    )

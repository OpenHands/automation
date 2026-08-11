"""Shared helpers for the `automation_service_metadata` key/value table.

Used for small singleton values that don't warrant their own table/column:
the PostHog backend distinct ID and frontend consent state (telemetry.py),
and git sync's last-synced-commit bookkeeping (git_sync/loop.py).
"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import AutomationServiceMetadata


async def get_service_metadata(session: AsyncSession, key: str) -> str | None:
    """Read a single automation_service_metadata value, or None if unset."""
    return await session.scalar(
        select(AutomationServiceMetadata.value).where(
            AutomationServiceMetadata.key == key
        )
    )


async def set_service_metadata(session: AsyncSession, key: str, value: str) -> None:
    """Upsert a single automation_service_metadata value."""
    await session.execute(
        text(
            "INSERT INTO automation_service_metadata (key, value) "
            "VALUES (:key, :value) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = excluded.value, updated_at = CURRENT_TIMESTAMP"
        ),
        {"key": key, "value": value},
    )

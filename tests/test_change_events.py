"""The audit hook: every ORM write of an automation records one change event."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from openhands.automation.models import (
    Automation,
    AutomationChangeActorKind,
    AutomationChangeEvent,
    AutomationOwnerKind,
    AutomationState,
    Base,
)
from openhands.automation.utils import change_events, utcnow
from openhands.automation.utils.change_events import AuditActor, set_audit_actor


ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
ACTOR = AuditActor(AutomationChangeActorKind.USER, USER_ID)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def strict(monkeypatch):
    """A flush with no actor raises, instead of logging and recording SYSTEM."""
    monkeypatch.setattr(change_events, "STRICT", True)


def _automation(**overrides) -> Automation:
    return Automation(
        user_id=USER_ID,
        org_id=ORG_ID,
        name="Audited",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run main.py",
        **overrides,
    )


async def _create(session_factory) -> uuid.UUID:
    async with session_factory() as session:
        set_audit_actor(session, ACTOR)
        automation = _automation()
        session.add(automation)
        await session.commit()
        return automation.id


async def _events(session_factory, automation_id):
    async with session_factory() as session:
        result = await session.execute(
            select(AutomationChangeEvent).where(
                AutomationChangeEvent.automation_id == automation_id
            )
        )
        return result.scalars().all()


async def _last(session_factory, automation_id) -> AutomationChangeEvent:
    """The one event after the create. SQLite stores whole seconds, so rows of
    one test share created_at; select by action instead of by order."""
    (event,) = [
        e for e in await _events(session_factory, automation_id) if e.action != "create"
    ]
    return event


async def _update(
    session_factory, automation_id, actor: AuditActor | None = ACTOR, **values
) -> None:
    async with session_factory() as session:
        if actor is not None:
            set_audit_actor(session, actor)
        automation = await session.get(Automation, automation_id)
        for column, value in values.items():
            setattr(automation, column, value)
        await session.commit()


async def test_insert_records_a_create_with_the_new_id(session_factory, strict):
    automation_id = await _create(session_factory)

    (event,) = await _events(session_factory, automation_id)
    assert event.action == "create"
    assert event.org_id == ORG_ID
    assert (event.actor_kind, event.actor_user_id) == ("user", USER_ID)
    assert "name" in event.changed_fields
    assert "updated_at" not in event.changed_fields


async def test_update_records_only_columns_whose_value_changed(session_factory, strict):
    automation_id = await _create(session_factory)

    # entrypoint is set to the value it already has.
    await _update(
        session_factory, automation_id, name="Renamed", entrypoint="uv run main.py"
    )

    event = await _last(session_factory, automation_id)
    assert (event.action, event.changed_fields) == ("update", ["name"])


async def test_same_value_or_bookkeeping_change_records_nothing(
    session_factory, strict
):
    automation_id = await _create(session_factory)

    await _update(session_factory, automation_id, name="Audited")
    await _update(session_factory, automation_id, last_polled_at=utcnow())

    assert len(await _events(session_factory, automation_id)) == 1


@pytest.mark.parametrize(
    ("values", "action"),
    [
        ({"enabled": False, "state": AutomationState.INACTIVE}, "disable"),
        ({"deleted_at": utcnow()}, "delete"),
        ({"owner_kind": AutomationOwnerKind.SERVICE}, "transfer"),
    ],
)
async def test_action_follows_from_the_changed_columns(
    session_factory, strict, values, action
):
    automation_id = await _create(session_factory)

    await _update(session_factory, automation_id, **values)

    assert (await _last(session_factory, automation_id)).action == action


async def test_git_sync_actor_prefixes_the_action(session_factory, strict):
    automation_id = await _create(session_factory)

    await _update(
        session_factory,
        automation_id,
        actor=AuditActor(AutomationChangeActorKind.GIT_SYNC),
        name="From git",
    )

    event = await _last(session_factory, automation_id)
    assert (event.action, event.actor_kind, event.actor_user_id) == (
        "git_sync_update",
        "git_sync",
        None,
    )


async def test_label_overrides_the_derived_action(session_factory, strict):
    automation_id = await _create(session_factory)

    async with session_factory() as session:
        set_audit_actor(session, ACTOR, action="regenerate")
        automation = await session.get(Automation, automation_id)
        automation.tarball_path = "s3://bucket/other.tar.gz"
        await session.commit()

    assert (await _last(session_factory, automation_id)).action == "regenerate"


async def test_flush_without_actor_raises_in_strict_mode(session_factory, strict):
    automation_id = await _create(session_factory)

    with pytest.raises(change_events.MissingAuditActorError):
        await _update(session_factory, automation_id, actor=None, name="No actor")


async def test_flush_without_actor_records_system_outside_strict_mode(
    session_factory, caplog
):
    automation_id = await _create(session_factory)

    await _update(session_factory, automation_id, actor=None, name="No actor")

    event = await _last(session_factory, automation_id)
    assert (event.actor_kind, event.actor_user_id) == ("system", None)
    assert "without an audit actor" in caplog.text

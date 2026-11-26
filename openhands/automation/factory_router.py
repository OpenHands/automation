"""FastAPI router for factories: named groups that automations can belong to.

An automation joins a factory when it is created (``factory_id`` on the create
endpoints) or through ``PATCH /v1/{automation_id}``; this router manages the
factories themselves.
"""

import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import (
    AuthenticatedUser,
    get_http_client,
    require_permission,
)
from openhands.automation.db import get_session, using_sqlite
from openhands.automation.models import Automation, AutomationOutcomeValue, Factory
from openhands.automation.outcome_values import MAX_OUTCOME_KEYS, validate_outcome_key
from openhands.automation.schemas import (
    FactoryCreate,
    FactoryListResponse,
    FactoryResponse,
    FactoryUpdate,
    OutcomeKeyDefinition,
)
from openhands.automation.utils.enterprise_client import notify_factory_deleted


router = APIRouter(prefix="/v1/factories", tags=["Factories"])

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")


def _live_automation_count():
    """Count of live automations, correlated to the outer Factory."""
    return (
        select(func.count(Automation.id))
        .where(Automation.factory_id == Factory.id, Automation.deleted_at.is_(None))
        .scalar_subquery()
    )


async def _count_live(session: AsyncSession, factory_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count(Automation.id)).where(
                Automation.factory_id == factory_id, Automation.deleted_at.is_(None)
            )
        )
        or 0
    )


def _to_response(factory: Factory, automation_count: int) -> FactoryResponse:
    return FactoryResponse(
        id=factory.id,
        name=factory.name,
        outcome_keys=_outcome_key_definitions(factory),
        automation_count=automation_count,
        created_at=factory.created_at,
        updated_at=factory.updated_at,
    )


async def get_org_factory(
    session: AsyncSession,
    org_id: uuid.UUID,
    factory_id: uuid.UUID,
    *,
    lock: Literal["update", "key_share"] | None = None,
) -> Factory:
    """Fetch a factory of the caller's org by id, or raise 404.

    ``lock`` takes a row lock on PostgreSQL: ``"update"`` for a delete,
    ``"key_share"`` for an assignment. They conflict with each other, but
    ``"key_share"`` does not block a rename.
    """
    query = select(Factory).where(Factory.org_id == org_id, Factory.id == factory_id)
    if lock == "update" and not using_sqlite():
        query = query.with_for_update()
    elif lock == "key_share" and not using_sqlite():
        query = query.with_for_update(read=True, key_share=True)
    factory = (await session.execute(query)).scalars().first()
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Factory not found"
        )
    return factory


async def factory_outcome_key_in_use(
    session: AsyncSession, factory_id: uuid.UUID, key: str
) -> bool:
    has_values = await session.scalar(
        select(
            exists().where(
                AutomationOutcomeValue.factory_id == factory_id,
                AutomationOutcomeValue.key == key,
            )
        )
    )
    if has_values:
        return True
    parser_rows = await session.scalars(
        select(Automation.outcome_parsers).where(
            Automation.factory_id == factory_id,
            Automation.outcome_parsers.is_not(None),
        )
    )
    return any(
        any(parser.get("key") == key for parser in (parsers or []))
        for parsers in parser_rows
    )


def _outcome_keys(factory: Factory) -> dict[str, dict[str, Any]]:
    return dict(factory.outcome_keys or {})


def _outcome_key_definitions(factory: Factory) -> dict[str, OutcomeKeyDefinition]:
    return {
        key: OutcomeKeyDefinition.model_validate(spec)
        for key, spec in _outcome_keys(factory).items()
    }


def _checked_outcome_key(key: str) -> str:
    try:
        return validate_outcome_key(key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _key_conflict(key: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Outcome key {key!r} is already in use",
    )


async def authorize_factory_change(
    session: AsyncSession, user: AuthenticatedUser, factory_id: uuid.UUID | None
) -> None:
    """Check that ``user`` may put an automation in ``factory_id``, or take it
    out of its factory when ``factory_id`` is None.

    Membership is a factory-level act: features built on factories hand a
    factory's automations access to the factory's data. It needs
    ``manage_automations`` (403), even for the automation's creator, and the
    factory must belong to the caller's org (404). This takes no lock; call
    ``lock_factory_for_assignment`` right before the write.
    """
    if "manage_automations" not in user.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins and owners can change an automation's factory",
        )
    if factory_id is not None:
        await get_org_factory(session, user.org_id, factory_id)


async def authorize_factory_for_create(
    session: AsyncSession, user: AuthenticatedUser, factory_id: uuid.UUID | None
) -> None:
    """``authorize_factory_change`` for a create request, where no factory
    asks for no check."""
    if factory_id is not None:
        await authorize_factory_change(session, user, factory_id)


def reject_factory_conflict(
    existing: Automation, requested_factory_id: uuid.UUID | None
) -> None:
    """Refuse a template re-enable that asks for a factory the existing
    automation is not in, rather than returning it as if the request applied.

    The re-enable stays a lookup with no write; ``PATCH`` moves the automation.
    """
    if requested_factory_id is None or existing.factory_id == requested_factory_id:
        return
    where = (
        f"in factory {existing.factory_id}"
        if existing.factory_id
        else "not in a factory"
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"This template is already enabled as automation {existing.id}, which "
            f"is {where}; change its factory with PATCH /v1/{existing.id}"
        ),
    )


async def lock_factory_for_assignment(
    session: AsyncSession, org_id: uuid.UUID, factory_id: uuid.UUID | None
) -> None:
    """Keep the factory from being deleted until this transaction ends, or raise
    404 if it is already gone. Nothing to lock when ``factory_id`` is None.

    Call it right before the write, not before slow calls, so the lock is short.
    SQLite has no row locks, so a no-op write takes its single write lock
    instead: a delete then runs wholly before this (404 here) or wholly after the
    commit (409 for the new member).
    """
    if factory_id is None:
        return
    if using_sqlite():
        result = await session.execute(
            update(Factory)
            .where(Factory.org_id == org_id, Factory.id == factory_id)
            .values(updated_at=Factory.updated_at)
            .returning(Factory.id)
            .execution_options(synchronize_session=False)
        )
        if result.first() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Factory not found"
            )
    else:
        await get_org_factory(session, org_id, factory_id, lock="key_share")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_factory(
    data: FactoryCreate,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Create a factory. A fresh install has none."""
    factory = Factory(
        org_id=user.org_id,
        name=data.name,
        outcome_keys={
            key: definition.model_dump(by_alias=True, exclude_defaults=True)
            for key, definition in data.outcome_keys.items()
        },
    )
    session.add(factory)
    await session.flush()
    await session.refresh(factory)
    return _to_response(factory, 0)


@router.get("")
async def list_factories(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> FactoryListResponse:
    """List the org's factories with their live automation counts."""
    total = (
        await session.scalar(
            select(func.count(Factory.id)).where(Factory.org_id == user.org_id)
        )
        or 0
    )
    rows = await session.execute(
        select(Factory, _live_automation_count())
        .where(Factory.org_id == user.org_id)
        .order_by(Factory.created_at.desc(), Factory.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return FactoryListResponse(
        factories=[_to_response(factory, count) for factory, count in rows.all()],
        total=total,
    )


@router.get("/{factory_id}")
async def get_factory(
    factory_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Get a factory."""
    factory = await get_org_factory(session, user.org_id, factory_id)
    count = await _count_live(session, factory.id)
    return _to_response(factory, count)


@router.patch("/{factory_id}")
async def rename_factory(
    factory_id: uuid.UUID,
    data: FactoryUpdate,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Rename a factory."""
    factory = await get_org_factory(session, user.org_id, factory_id)
    factory.name = data.name
    await session.flush()
    await session.refresh(factory)
    count = await _count_live(session, factory.id)
    return _to_response(factory, count)


@router.get("/{factory_id}/outcome-keys")
async def get_factory_outcome_keys(
    factory_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, OutcomeKeyDefinition]:
    factory = await get_org_factory(session, user.org_id, factory_id)
    return _outcome_key_definitions(factory)


@router.put("/{factory_id}/outcome-keys/{key}")
async def put_factory_outcome_key(
    factory_id: uuid.UUID,
    key: str,
    data: OutcomeKeyDefinition,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    _checked_outcome_key(key)
    factory = await get_org_factory(session, user.org_id, factory_id, lock="update")
    keys = _outcome_keys(factory)
    current = OutcomeKeyDefinition.model_validate(keys[key]) if key in keys else None
    if (
        current is not None
        and current != data
        and await factory_outcome_key_in_use(session, factory.id, key)
    ):
        raise _key_conflict(key)
    if current is None and len(keys) >= MAX_OUTCOME_KEYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Too many outcome keys"
        )
    keys[key] = data.model_dump(by_alias=True, exclude_defaults=True)
    factory.outcome_keys = keys
    await session.flush()
    await session.refresh(factory)
    count = await _count_live(session, factory.id)
    return _to_response(factory, count)


@router.delete(
    "/{factory_id}/outcome-keys/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_factory_outcome_key(
    factory_id: uuid.UUID,
    key: str,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    _checked_outcome_key(key)
    factory = await get_org_factory(session, user.org_id, factory_id, lock="update")
    keys = _outcome_keys(factory)
    if key not in keys:
        return
    if await factory_outcome_key_in_use(session, factory.id, key):
        raise _key_conflict(key)
    keys.pop(key)
    factory.outcome_keys = keys
    await session.flush()


@router.delete("/{factory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_factory(
    factory_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> None:
    """Delete a factory that no live automation belongs to.

    Soft-deleted automations that still reference it are detached in the same
    transaction. After it commits, the OpenHands server is told so it can remove
    what it keeps for the factory; that notification never blocks or undoes the
    delete.
    """
    factory = await get_org_factory(session, user.org_id, factory_id, lock="update")

    # Loaded and set through the ORM, not left to the foreign key's SET NULL, so
    # each detach reaches the automation change audit.
    detached = await session.scalars(
        select(Automation).where(
            Automation.factory_id == factory.id, Automation.deleted_at.is_not(None)
        )
    )
    for automation in detached:
        automation.factory_id = None
    await session.flush()

    # One statement checks for live members and deletes, so on SQLite an
    # assignment cannot commit between the two. On PostgreSQL the row lock above
    # already holds assignments back, and this statement reads after it.
    deleted = await session.execute(
        delete(Factory)
        .where(
            Factory.id == factory.id,
            ~exists().where(
                Automation.factory_id == factory.id, Automation.deleted_at.is_(None)
            ),
        )
        .returning(Factory.id)
        .execution_options(synchronize_session=False)
    )
    if deleted.first() is None:
        live = await _count_live(session, factory.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Factory has {live} live automation{'s' if live != 1 else ''}; "
                "remove them from it first"
            ),
        )
    await session.commit()
    background_tasks.add_task(notify_factory_deleted, client, user.org_id, factory_id)

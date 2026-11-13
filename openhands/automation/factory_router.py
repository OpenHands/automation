"""FastAPI router for factories: named groups that automations can belong to.

An automation joins a factory through ``PATCH /v1/{automation_id}`` (see
``router.update_automation``); this router manages the factories themselves.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.db import get_session, using_sqlite
from openhands.automation.factories import slugify_factory_name
from openhands.automation.models import Automation, Factory
from openhands.automation.schemas import (
    FactoryCreate,
    FactoryListResponse,
    FactoryResponse,
    FactoryUpdate,
)


router = APIRouter(prefix="/v1/factories", tags=["Factories"])

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")


def _live_automation_count(factory_id: uuid.UUID | None = None):
    """Count of a factory's live automations, correlated to the outer Factory."""
    return (
        select(func.count(Automation.id))
        .where(
            Automation.factory_id == (factory_id or Factory.id),
            Automation.deleted_at.is_(None),
        )
        .scalar_subquery()
    )


async def _count_live(session: AsyncSession, factory_id: uuid.UUID) -> int:
    return await session.scalar(select(_live_automation_count(factory_id))) or 0


def _to_response(factory: Factory, automation_count: int) -> FactoryResponse:
    return FactoryResponse(
        id=factory.id,
        name=factory.name,
        slug=factory.slug,
        automation_count=automation_count,
        created_at=factory.created_at,
        updated_at=factory.updated_at,
    )


async def _get_org_factory(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    slug: str | None = None,
    factory_id: uuid.UUID | None = None,
    lock: str | None = None,
) -> Factory:
    """Fetch a factory of the caller's org by slug or id, or raise 404.

    ``lock`` is ``"update"`` or ``"share"`` and takes a row lock on PostgreSQL.
    Deleting a factory locks it for update and assigning an automation locks it
    for share, so an assignment cannot slip in between the delete's count of live
    automations and the row's removal. SQLite has a single writer and no row
    locks.
    """
    query = select(Factory).where(Factory.org_id == org_id)
    if slug is not None:
        query = query.where(Factory.slug == slug)
    if factory_id is not None:
        query = query.where(Factory.id == factory_id)
    if lock is not None and not using_sqlite():
        query = query.with_for_update(read=lock == "share")
    factory = (await session.execute(query)).scalars().first()
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Factory not found"
        )
    return factory


async def get_org_factory_for_assignment(
    session: AsyncSession, org_id: uuid.UUID, factory_id: uuid.UUID
) -> Factory:
    """The factory an automation is being assigned to; 404 when it does not
    exist or belongs to another org."""
    return await _get_org_factory(session, org_id, factory_id=factory_id, lock="share")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_factory(
    data: FactoryCreate,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Create a factory. A fresh install has none."""
    try:
        slug = data.slug or slugify_factory_name(data.name)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    factory = Factory(org_id=user.org_id, name=data.name, slug=slug)
    session.add(factory)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Factory slug '{slug}' already exists in this org",
        )
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


@router.get("/{slug}")
async def get_factory(
    slug: str,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Get a factory by slug."""
    factory = await _get_org_factory(session, user.org_id, slug=slug)
    count = await _count_live(session, factory.id)
    return _to_response(factory, count)


@router.patch("/{slug}")
async def rename_factory(
    slug: str,
    data: FactoryUpdate,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Rename a factory. The slug never changes."""
    factory = await _get_org_factory(session, user.org_id, slug=slug)
    factory.name = data.name
    await session.flush()
    await session.refresh(factory)
    count = await _count_live(session, factory.id)
    return _to_response(factory, count)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_factory(
    slug: str,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    """Delete a factory that no live automation belongs to.

    Soft-deleted automations that still reference it are detached in the same
    transaction.
    """
    factory = await _get_org_factory(session, user.org_id, slug=slug, lock="update")
    live = await _count_live(session, factory.id)
    if live:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Factory has {live} live automation{'s' if live != 1 else ''}; "
                "remove them from it first"
            ),
        )

    # Loaded and set through the ORM, not left to the foreign key's SET NULL, so
    # each detach reaches the automation change audit.
    detached = await session.scalars(
        select(Automation).where(Automation.factory_id == factory.id)
    )
    for automation in detached:
        automation.factory_id = None
    await session.flush()
    await session.delete(factory)
    await session.flush()

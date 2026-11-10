"""Factories: named groups of automations.

Endpoints (all under ``/v1/factories``):

- ``POST ""``, ``GET ""``, ``GET /{slug}``, ``PATCH /{slug}`` (rename) and
  ``DELETE /{slug}``.

Reading is open to any org member. Creating, renaming and deleting need
``manage_automations``, like assigning an automation to a factory (see
``router.update_automation``). The slug is the URL identifier and never changes.
"""

import re
import unicodedata
import uuid
from collections.abc import Iterable
from typing import Final

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import (
    AuthenticatedUser,
    get_http_client,
    require_permission,
)
from openhands.automation.db import get_session
from openhands.automation.models import Automation, Factory
from openhands.automation.schemas import (
    FACTORY_SLUG_MAX_LENGTH,
    CreateFactoryRequest,
    FactoryListResponse,
    FactoryResponse,
    UpdateFactoryRequest,
)
from openhands.automation.utils.enterprise_client import purge_factory_worklogs


router = APIRouter(prefix="/v1/factories", tags=["Factories"])

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")

_NON_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def derive_slug(name: str) -> str:
    """Lowercase ASCII words of ``name`` joined by single hyphens (may be empty)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = _NON_SLUG_RE.sub("-", ascii_name.lower()).strip("-")
    return slug[:FACTORY_SLUG_MAX_LENGTH].strip("-")


async def _automation_counts(
    session: AsyncSession, factory_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Live (not soft-deleted) automations per factory; absent means none."""
    ids = list(factory_ids)
    if not ids:
        return {}
    result = await session.execute(
        select(Automation.factory_id, func.count())
        .where(Automation.factory_id.in_(ids), Automation.deleted_at.is_(None))
        .group_by(Automation.factory_id)
    )
    return {factory_id: count for factory_id, count in result.all()}


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
    session: AsyncSession, org_id: uuid.UUID, slug: str
) -> Factory:
    factory = (
        await session.execute(
            select(Factory).where(Factory.org_id == org_id, Factory.slug == slug)
        )
    ).scalar_one_or_none()
    if factory is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Factory not found")
    return factory


def _slug_taken(slug: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail=f"Factory slug '{slug}' already exists for this org",
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_factory(
    body: CreateFactoryRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Create a factory. The slug is derived from the name when omitted."""
    slug = body.slug or derive_slug(body.name)
    if not slug:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A slug cannot be derived from this name; provide one",
        )
    taken = await session.scalar(
        select(Factory.id).where(Factory.org_id == user.org_id, Factory.slug == slug)
    )
    if taken is not None:
        raise _slug_taken(slug)

    factory = Factory(org_id=user.org_id, name=body.name, slug=slug)
    session.add(factory)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _slug_taken(slug) from exc
    await session.refresh(factory)
    return _to_response(factory, 0)


@router.get("")
async def list_factories(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryListResponse:
    """List the caller's org's factories by name."""
    total = await session.scalar(
        select(func.count()).select_from(Factory).where(Factory.org_id == user.org_id)
    )
    factories = (
        (
            await session.execute(
                select(Factory)
                .where(Factory.org_id == user.org_id)
                .order_by(func.lower(Factory.name), Factory.id)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    counts = await _automation_counts(session, (f.id for f in factories))
    return FactoryListResponse(
        factories=[_to_response(f, counts.get(f.id, 0)) for f in factories],
        total=total or 0,
    )


@router.get("/{slug}")
async def get_factory(
    slug: str,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    factory = await _get_org_factory(session, user.org_id, slug)
    counts = await _automation_counts(session, [factory.id])
    return _to_response(factory, counts.get(factory.id, 0))


@router.patch("/{slug}")
async def rename_factory(
    slug: str,
    body: UpdateFactoryRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> FactoryResponse:
    """Rename a factory. Its slug stays the same."""
    factory = await _get_org_factory(session, user.org_id, slug)
    factory.name = body.name
    await session.flush()
    await session.refresh(factory)
    counts = await _automation_counts(session, [factory.id])
    return _to_response(factory, counts.get(factory.id, 0))


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_factory(
    slug: str,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> None:
    """Delete a factory that no live automation belongs to.

    Soft-deleted automations that still point at it are detached first, so the
    outcome does not depend on the database enforcing the foreign key. Once the
    delete commits, the enterprise server is told to drop the factory's worklogs.
    """
    factory = await _get_org_factory(session, user.org_id, slug)
    live = (await _automation_counts(session, [factory.id])).get(factory.id, 0)
    if live:
        noun = "automation" if live == 1 else "automations"
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"Factory still has {live} live {noun}; "
                "remove them from the factory before deleting it"
            ),
        )
    await session.execute(
        update(Automation)
        .where(Automation.factory_id == factory.id)
        .values(factory_id=None)
        .execution_options(synchronize_session=False)
    )
    await session.delete(factory)
    background_tasks.add_task(purge_factory_worklogs, client, user.org_id, factory.id)

"""FastAPI router for outbound WebSocket source management.

Provides CRUD operations for outbound WebSocket connections.  The actual
connection lifecycle is managed by SocketManager (socket_manager.py), which
is notified via app.state when sources are created, updated, or deleted.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, authenticate_request
from openhands.automation.db import get_session
from openhands.automation.models import OutboundWebSocketSource, WebSocketStatus
from openhands.automation.schemas import (
    WebSocketSourceCreate,
    WebSocketSourceListResponse,
    WebSocketSourceResponse,
    WebSocketSourceStatus,
    WebSocketSourceUpdate,
)


logger = logging.getLogger("automation.websocket_source_router")

router = APIRouter(prefix="/v1/websocket-sources", tags=["WebSocket Sources"])


def _to_response(source: OutboundWebSocketSource) -> WebSocketSourceResponse:
    """Convert ORM model to response schema, redacting credentials."""
    return WebSocketSourceResponse(
        id=source.id,
        org_id=source.org_id,
        name=source.name,
        source=source.source,
        kind=source.kind,
        enabled=source.enabled,
        event_key_expr=source.event_key_expr,
        payload_expr=source.payload_expr,
        filter_expr=source.filter_expr,
        url=source.url,
        # headers and app_token are intentionally not returned
        status=WebSocketSourceStatus(source.status.value),
        status_detail=source.status_detail,
        connected_at=source.connected_at,
        last_event_at=source.last_event_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _get_socket_manager(request: Request):
    """Return the SocketManager from app state, or None if not initialised."""
    return getattr(request.app.state, "socket_manager", None)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_websocket_source(
    data: WebSocketSourceCreate,
    request: Request,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> WebSocketSourceResponse:
    """
    Register a new outbound WebSocket source.

    On success the SocketManager opens a connection immediately (if the source
    is enabled).
    """
    source = OutboundWebSocketSource(
        org_id=auth.org_id,
        name=data.name,
        source=data.source,
        kind=data.kind,
        enabled=data.enabled,
        event_key_expr=data.event_key_expr,
        payload_expr=data.payload_expr,
        filter_expr=data.filter_expr,
        status=WebSocketStatus.DISCONNECTED,
        # kind-specific fields
        url=getattr(data, "url", None),
        headers=getattr(data, "headers", None),
        app_token=getattr(data, "app_token", None),
    )
    session.add(source)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"WebSocket source '{data.source}' already exists for this org",
        )

    await session.refresh(source)

    # Notify the socket manager to open a connection for this new source
    if source.enabled:
        sm = _get_socket_manager(request)
        if sm is not None:
            await sm.on_source_changed(source.id)

    logger.info("Created WebSocket source id=%s source=%s", source.id, source.source)
    return _to_response(source)


@router.get("")
async def list_websocket_sources(
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> WebSocketSourceListResponse:
    """List all outbound WebSocket sources for the organisation."""
    count_q = select(func.count()).select_from(
        select(OutboundWebSocketSource.id)
        .where(OutboundWebSocketSource.org_id == auth.org_id)
        .subquery()
    )
    total = await session.scalar(count_q) or 0

    q = (
        select(OutboundWebSocketSource)
        .where(OutboundWebSocketSource.org_id == auth.org_id)
        .order_by(OutboundWebSocketSource.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(q)
    sources = result.scalars().all()

    return WebSocketSourceListResponse(
        sources=[_to_response(s) for s in sources],
        total=total,
    )


@router.get("/{source_id}")
async def get_websocket_source(
    source_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> WebSocketSourceResponse:
    """Get details of a specific WebSocket source, including live connection status."""
    source = await session.get(OutboundWebSocketSource, source_id)
    if not source or source.org_id != auth.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _to_response(source)


@router.patch("/{source_id}")
async def update_websocket_source(
    source_id: uuid.UUID,
    data: WebSocketSourceUpdate,
    request: Request,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> WebSocketSourceResponse:
    """
    Update a WebSocket source's configuration.

    Changing ``enabled``, ``url``, ``headers``, or ``app_token`` triggers an
    immediate reconnect (or disconnect if being disabled).
    The ``kind`` and ``source`` slug are immutable.
    """
    source = await session.get(OutboundWebSocketSource, source_id)
    if not source or source.org_id != auth.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    update_data = data.model_dump(exclude_unset=True)
    connection_affecting = {"enabled", "url", "headers", "app_token"}
    needs_reconnect = bool(update_data.keys() & connection_affecting)

    for field, value in update_data.items():
        setattr(source, field, value)

    # Validate kind-specific required fields remain set after applying updates.
    # This guards against a caller clearing url on a generic source or
    # app_token on a slack source, which would cause runtime failures in the
    # SocketManager on the next connect attempt.
    if source.kind == "generic" and not source.url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="url is required for generic sources and cannot be cleared",
        )
    if source.kind == "slack" and not source.app_token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="app_token is required for slack sources and cannot be cleared",
        )

    await session.commit()
    await session.refresh(source)

    if needs_reconnect:
        sm = _get_socket_manager(request)
        if sm is not None:
            await sm.on_source_changed(source.id)

    return _to_response(source)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_websocket_source(
    source_id: uuid.UUID,
    request: Request,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a WebSocket source and close its connection."""
    source = await session.get(OutboundWebSocketSource, source_id)
    if not source or source.org_id != auth.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    sm = _get_socket_manager(request)
    if sm is not None:
        await sm.on_source_deleted(source_id)

    await session.delete(source)
    await session.commit()


@router.post("/{source_id}/reconnect", status_code=status.HTTP_202_ACCEPTED)
async def reconnect_websocket_source(
    source_id: uuid.UUID,
    request: Request,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> WebSocketSourceResponse:
    """Force an immediate reconnect for a WebSocket source."""
    source = await session.get(OutboundWebSocketSource, source_id)
    if not source or source.org_id != auth.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not source.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source is disabled; enable it before reconnecting",
        )

    sm = _get_socket_manager(request)
    if sm is not None:
        await sm.on_source_changed(source_id)

    await session.refresh(source)
    return _to_response(source)

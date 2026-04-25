"""FastAPI router for the automation KV store API.

Provides a Redis-like key-value store scoped per-automation for state persistence.
All values are encrypted at the application level using JWE.
Authentication is via per-run JWT tokens (AUTOMATION_KV_TOKEN).
"""

import logging
import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
    status,
)
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import get_settings
from automation.db import get_session
from automation.kv_helpers import (
    get_nested_value,
    require_dict,
    require_int,
    require_list,
    require_numeric,
    safe_decrypt,
    safe_encrypt,
    set_nested_value,
    validate_key,
)
from automation.kv_schemas import (
    KVConflictResponse,
    KVDeleteResponse,
    KVIncrRequest,
    KVIncrResponse,
    KVKeyMetaResponse,
    KVKeyPathResponse,
    KVKeyResponse,
    KVListKeysResponse,
    KVListLengthResponse,
    KVListPushRequest,
    KVPatchRequest,
    KVSetResponse,
)
from automation.models import AutomationKV
from automation.utils.kv import KVTokenError, verify_kv_token


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kv", tags=["KV Store"])


# --- Authentication ---


async def get_automation_id_from_token(
    authorization: Annotated[str, Header()],
) -> uuid.UUID:
    """Extract and verify the automation_id from the KV token.

    The token is passed via Authorization: Bearer <token> header.
    It contains the automation_id as a trusted claim.
    """
    settings = get_settings()

    if not settings.kv_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="KV store not configured (missing AUTOMATION_KV_SECRET)",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format",
        )

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
        )

    try:
        return verify_kv_token(settings.kv_secret, token)
    except KVTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


# --- Validation Helpers ---


# Type alias for validated KV keys - ensures key validation is applied
# Use this as a FastAPI path parameter annotation: key: ValidatedKey
ValidatedKey = Annotated[str, Depends(lambda key: validate_key(key))]


def _check_value_size(value: Any, settings=None) -> None:
    """Validate that a value doesn't exceed the configured size limit.

    Args:
        value: The value to check (will be JSON-serialized to measure size)
        settings: Optional settings object (fetched if not provided)

    Raises:
        HTTPException: 413 Payload Too Large if value exceeds limit
    """
    import json

    if settings is None:
        settings = get_settings()

    max_size = settings.kv_max_value_size
    if max_size <= 0:
        return  # Size limit disabled

    # Measure the JSON-serialized size (this is what gets encrypted/stored)
    try:
        serialized = json.dumps(value)
    except (TypeError, ValueError):
        # If we can't serialize it, the encrypt step will fail anyway
        return

    actual_size = len(serialized.encode("utf-8"))
    if actual_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Value size ({actual_size} bytes) exceeds limit ({max_size} bytes)",
        )


# --- Database Helpers ---


async def _get_kv_row(
    session: AsyncSession,
    automation_id: uuid.UUID,
    key: ValidatedKey,
) -> AutomationKV | None:
    """Get a KV row by automation_id and key."""
    result = await session.execute(
        select(AutomationKV).where(
            AutomationKV.automation_id == automation_id,
            AutomationKV.key == key,
        )
    )
    return result.scalars().first()


async def _get_kv_row_for_update(
    session: AsyncSession,
    automation_id: uuid.UUID,
    key: ValidatedKey,
) -> AutomationKV | None:
    """Get a KV row with FOR UPDATE lock."""
    result = await session.execute(
        select(AutomationKV)
        .where(
            AutomationKV.automation_id == automation_id,
            AutomationKV.key == key,
        )
        .with_for_update()
    )
    return result.scalars().first()


# --- Endpoints ---


@router.get("")
async def list_keys(
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVListKeysResponse:
    """List all keys for this automation."""
    result = await session.execute(
        select(AutomationKV.key).where(AutomationKV.automation_id == automation_id)
    )
    keys = [row[0] for row in result.all()]
    return KVListKeysResponse(keys=keys, count=len(keys))


@router.get("/{key}")
async def get_value(
    key: ValidatedKey,
    path: str | None = Query(default=None, description="Nested path (dot notation)"),
    meta: bool = Query(default=False, description="Include metadata"),
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse | KVKeyPathResponse | KVKeyMetaResponse:
    """Get a value by key, optionally at a nested path."""
    settings = get_settings()

    kv = await _get_kv_row(session, automation_id, key)
    if kv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    if path:
        try:
            value = get_nested_value(value, path)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="invalid_path",
            )
        return KVKeyPathResponse(key=key, path=path, value=value)

    if meta:
        return KVKeyMetaResponse(
            key=key,
            value=value,
            created_at=kv.created_at.isoformat(),
            updated_at=kv.updated_at.isoformat(),
        )

    return KVKeyResponse(key=key, value=value)


@router.put("/{key}")
async def set_value(
    key: ValidatedKey,
    body: Annotated[Any, Body()],  # Accept any JSON body directly as the value
    response: Response,
    nx: bool = Query(default=False, description="Only set if key does not exist"),
    xx: bool = Query(default=False, description="Only set if key exists"),
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVSetResponse | KVConflictResponse:
    """Set a value for a key.

    The entire request body is stored as the value.

    Query params:
    - nx=true: Only set if key does NOT exist (like Redis SETNX)
    - xx=true: Only set if key DOES exist

    Returns:
    - 200: Key updated (existing key)
    - 201: Key created (new key, or nx=true success)
    - 409: Conflict (nx=true but key exists, or xx=true but key doesn't exist)
    - 413: Payload too large (value exceeds size limit)
    """
    settings = get_settings()

    if nx and xx:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot use both nx and xx",
        )

    _check_value_size(body, settings)

    encrypted = safe_encrypt(settings.kv_secret, body)

    if nx:
        # SETNX: only set if key doesn't exist
        stmt = (
            pg_insert(AutomationKV)
            .values(
                automation_id=automation_id,
                key=key,
                value_encrypted=encrypted,
            )
            .on_conflict_do_nothing(index_elements=["automation_id", "key"])
            .returning(AutomationKV)
        )
        result = await session.execute(stmt)
        row = result.scalars().first()

        if row is None:
            # Key already existed - return 409 Conflict
            response.status_code = status.HTTP_409_CONFLICT
            return KVConflictResponse(key=key, created=False, error="key_exists")

        # Key was created - return 201 Created
        response.status_code = status.HTTP_201_CREATED
        return KVSetResponse(
            key=key,
            value=body,
            created=True,
            updated_at=row.updated_at.isoformat(),
        )

    if xx:
        # Only set if key exists
        kv = await _get_kv_row(session, automation_id, key)
        if kv is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="key_not_exists",
            )
        kv.value_encrypted = encrypted
        await session.flush()
        await session.refresh(kv)
        return KVSetResponse(
            key=key,
            value=body,
            created=False,
            updated_at=kv.updated_at.isoformat(),
        )

    # Check if key exists first to determine insert vs update
    existing = await _get_kv_row(session, automation_id, key)
    created = existing is None

    # Normal upsert - use func.now() to properly update the timestamp
    stmt = (
        pg_insert(AutomationKV)
        .values(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        .on_conflict_do_update(
            index_elements=["automation_id", "key"],
            set_={"value_encrypted": encrypted, "updated_at": func.now()},
        )
        .returning(AutomationKV.updated_at)
    )
    result = await session.execute(stmt)
    row = result.first()

    # Return 201 for new keys, 200 for updates
    if created:
        response.status_code = status.HTTP_201_CREATED

    return KVSetResponse(
        key=key,
        value=body,
        created=created,
        updated_at=row.updated_at.isoformat() if row else "",
    )


@router.patch("/{key}")
async def patch_value(
    key: ValidatedKey,
    body: KVPatchRequest,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyPathResponse:
    """Update a nested path within an existing value."""
    settings = get_settings()

    kv = await _get_kv_row_for_update(session, automation_id, key)
    if kv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    require_dict(value)

    try:
        set_nested_value(value, body.path, body.value)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid_path: {e}",
        )

    # Check size of the updated value before encrypting
    _check_value_size(value, settings)

    kv.value_encrypted = safe_encrypt(settings.kv_secret, value)

    await session.flush()
    await session.refresh(kv)

    return KVKeyPathResponse(
        key=key,
        path=body.path,
        value=body.value,
    )


@router.delete("/{key}")
async def delete_key(
    key: ValidatedKey,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVDeleteResponse:
    """Delete a key."""
    result = await session.execute(
        delete(AutomationKV).where(
            AutomationKV.automation_id == automation_id,
            AutomationKV.key == key,
        )
    )
    deleted = result.rowcount > 0  # type: ignore[union-attr]
    return KVDeleteResponse(key=key, deleted=deleted)


@router.post("/{key}/incr")
async def increment(
    key: ValidatedKey,
    body: KVIncrRequest | None = None,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVIncrResponse:
    """Atomically increment an integer value.

    If the key doesn't exist, initializes it to `by` (default 1).

    Note: The stored value must be an integer. Float values are rejected
    because integer arithmetic on floats can cause precision loss.
    """
    settings = get_settings()
    by = body.by if body else 1

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        # Initialize with `by`
        encrypted = safe_encrypt(settings.kv_secret, by)

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVIncrResponse(key=key, value=by)

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    # Require integer, not just numeric - floats would lose precision with int()
    require_int(value)

    new_value = value + by

    kv.value_encrypted = safe_encrypt(settings.kv_secret, new_value)

    await session.flush()
    return KVIncrResponse(key=key, value=new_value)


@router.post("/{key}/decr")
async def decrement(
    key: ValidatedKey,
    body: KVIncrRequest | None = None,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVIncrResponse:
    """Atomically decrement an integer value.

    If the key doesn't exist, initializes it to `-by` (default -1).

    Note: The stored value must be an integer. Float values are rejected
    because integer arithmetic on floats can cause precision loss.
    """
    settings = get_settings()
    by = body.by if body else 1

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        # Initialize with `-by`
        encrypted = safe_encrypt(settings.kv_secret, -by)

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVIncrResponse(key=key, value=-by)

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    # Require integer, not just numeric - floats would lose precision
    require_int(value)

    new_value = value - by

    kv.value_encrypted = safe_encrypt(settings.kv_secret, new_value)

    await session.flush()
    return KVIncrResponse(key=key, value=new_value)


@router.post("/{key}/lpush")
async def lpush(
    key: ValidatedKey,
    body: KVListPushRequest,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVListLengthResponse:
    """Push a value to the left (front) of a list.

    Creates the list if it doesn't exist.
    """
    settings = get_settings()

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        # Initialize with single-element list
        value = [body.value]
        _check_value_size(value, settings)
        encrypted = safe_encrypt(settings.kv_secret, value)

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVListLengthResponse(key=key, length=1)

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    require_list(value)

    value.insert(0, body.value)
    _check_value_size(value, settings)

    kv.value_encrypted = safe_encrypt(settings.kv_secret, value)

    await session.flush()
    return KVListLengthResponse(key=key, length=len(value))


@router.post("/{key}/rpush")
async def rpush(
    key: ValidatedKey,
    body: KVListPushRequest,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVListLengthResponse:
    """Push a value to the right (back) of a list.

    Creates the list if it doesn't exist.
    """
    settings = get_settings()

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        # Initialize with single-element list
        value = [body.value]
        _check_value_size(value, settings)
        encrypted = safe_encrypt(settings.kv_secret, value)

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVListLengthResponse(key=key, length=1)

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    require_list(value)

    value.append(body.value)
    _check_value_size(value, settings)

    kv.value_encrypted = safe_encrypt(settings.kv_secret, value)

    await session.flush()
    return KVListLengthResponse(key=key, length=len(value))


@router.post("/{key}/lpop")
async def lpop(
    key: ValidatedKey,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse:
    """Pop a value from the left (front) of a list.

    Returns null if list is empty.
    """
    settings = get_settings()

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        return KVKeyResponse(key=key, value=None)

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    require_list(value)

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop(0)

    kv.value_encrypted = safe_encrypt(settings.kv_secret, value)

    await session.flush()
    return KVKeyResponse(key=key, value=popped)


@router.post("/{key}/rpop")
async def rpop(
    key: ValidatedKey,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse:
    """Pop a value from the right (back) of a list.

    Returns null if list is empty.
    """
    settings = get_settings()

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        return KVKeyResponse(key=key, value=None)

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    require_list(value)

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop()

    kv.value_encrypted = safe_encrypt(settings.kv_secret, value)

    await session.flush()
    return KVKeyResponse(key=key, value=popped)


@router.get("/{key}/len")
async def list_length(
    key: ValidatedKey,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVListLengthResponse:
    """Get the length of a list."""
    settings = get_settings()

    kv = await _get_kv_row(session, automation_id, key)

    if kv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = safe_decrypt(settings.kv_secret, kv.value_encrypted)

    require_list(value)

    return KVListLengthResponse(key=key, length=len(value))

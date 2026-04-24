"""FastAPI router for the automation KV store API.

Provides a Redis-like key-value store scoped per-automation for state persistence.
All values are encrypted at the application level using JWE.
Authentication is via per-run JWT tokens (AUTOMATION_KV_TOKEN).
"""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import get_settings
from automation.db import get_session
from automation.models import AutomationKV
from automation.utils.kv import (
    KVEncryptionError,
    KVTokenError,
    decrypt_value,
    encrypt_value,
    verify_kv_token,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kv", tags=["KV Store"])


# --- Request/Response Schemas ---


class KVSetRequest(BaseModel):
    """Request body for setting a KV value (used when body is explicit)."""

    value: Any = Field(..., description="Any JSON-serializable value")


class KVPatchRequest(BaseModel):
    """Request body for patching a nested path."""

    path: str = Field(
        ..., description="Dot-notation path to update (e.g., 'database.port')"
    )
    value: Any = Field(..., description="Value to set at the path")


class KVIncrRequest(BaseModel):
    """Request body for increment/decrement operations."""

    by: int = Field(default=1, description="Amount to increment/decrement by")


class KVListPushRequest(BaseModel):
    """Request body for list push operations."""

    value: Any = Field(..., description="Value to push onto the list")


class KVKeyResponse(BaseModel):
    """Response containing a key and its value."""

    key: str
    value: Any


class KVKeyPathResponse(BaseModel):
    """Response containing a key, path, and value."""

    key: str
    path: str
    value: Any


class KVKeyMetaResponse(BaseModel):
    """Response containing a key, value, and metadata."""

    key: str
    value: Any
    created_at: str
    updated_at: str


class KVSetResponse(BaseModel):
    """Response for set operations."""

    key: str
    value: Any
    created: bool
    updated_at: str


class KVDeleteResponse(BaseModel):
    """Response for delete operations."""

    key: str
    deleted: bool


class KVListKeysResponse(BaseModel):
    """Response for listing keys."""

    keys: list[str]
    count: int


class KVIncrResponse(BaseModel):
    """Response for increment/decrement operations."""

    key: str
    value: int


class KVListLengthResponse(BaseModel):
    """Response for list length operations."""

    key: str
    length: int


class KVConflictResponse(BaseModel):
    """Response when a conditional operation fails."""

    key: str
    created: bool = False
    error: str


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


# --- Helpers ---


def _get_nested_value(obj: Any, path: str) -> Any:
    """Get a value at a nested path using dot notation.

    Supports bracket notation for keys with dots: config["my.key"]
    """
    if not path:
        return obj

    parts = _parse_path(path)
    current = obj

    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"Path '{path}' not found")
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                raise KeyError(f"Path '{path}' not found")
        else:
            raise KeyError(f"Path '{path}' not found")

    return current


def _set_nested_value(obj: dict, path: str, value: Any) -> dict:
    """Set a value at a nested path using dot notation.

    Creates intermediate dicts as needed.
    """
    parts = _parse_path(path)
    current = obj

    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = current[part]
        if not isinstance(current, dict):
            raise ValueError(
                f"Cannot set path '{path}': intermediate value is not a dict"
            )

    current[parts[-1]] = value
    return obj


def _parse_path(path: str) -> list[str]:
    """Parse a path string into parts.

    Supports:
    - Dot notation: database.host
    - Bracket notation: config["my.key.with.dots"]
    """
    parts = []
    current = ""
    i = 0

    while i < len(path):
        char = path[i]

        if char == ".":
            if current:
                parts.append(current)
                current = ""
        elif char == "[":
            if current:
                parts.append(current)
                current = ""
            # Find closing bracket
            end = path.find("]", i)
            if end == -1:
                raise ValueError(f"Invalid path: unclosed bracket in '{path}'")
            # Extract key (strip quotes if present)
            key = path[i + 1 : end]
            if key.startswith('"') and key.endswith('"'):
                key = key[1:-1]
            elif key.startswith("'") and key.endswith("'"):
                key = key[1:-1]
            parts.append(key)
            i = end
        else:
            current += char

        i += 1

    if current:
        parts.append(current)

    return parts


async def _get_kv_row(
    session: AsyncSession,
    automation_id: uuid.UUID,
    key: str,
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
    key: str,
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
    key: str,
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

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if path:
        try:
            value = _get_nested_value(value, path)
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
    key: str,
    body: Annotated[Any, Body()],  # Accept any JSON body directly as the value
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
    """
    settings = get_settings()

    if nx and xx:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot use both nx and xx",
        )

    try:
        encrypted = encrypt_value(settings.kv_secret, body)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

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
            # Key already existed
            return KVConflictResponse(key=key, created=False, error="key_exists")

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

    # Normal upsert
    stmt = (
        pg_insert(AutomationKV)
        .values(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        .on_conflict_do_update(
            index_elements=["automation_id", "key"],
            set_={"value_encrypted": encrypted, "updated_at": AutomationKV.updated_at},
        )
        .returning(AutomationKV.created_at, AutomationKV.updated_at)
    )
    result = await session.execute(stmt)
    row = result.first()

    # Check if this was an insert or update by comparing timestamps
    created = row is not None and row.created_at == row.updated_at

    return KVSetResponse(
        key=key,
        value=body,
        created=created,
        updated_at=row.updated_at.isoformat() if row else "",
    )


@router.patch("/{key}")
async def patch_value(
    key: str,
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

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not an object",
        )

    try:
        _set_nested_value(value, body.path, body.value)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid_path: {e}",
        )

    try:
        kv.value_encrypted = encrypt_value(settings.kv_secret, value)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

    await session.flush()
    await session.refresh(kv)

    return KVKeyPathResponse(
        key=key,
        path=body.path,
        value=body.value,
    )


@router.delete("/{key}")
async def delete_key(
    key: str,
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
    key: str,
    body: KVIncrRequest | None = None,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVIncrResponse:
    """Atomically increment a numeric value.

    If the key doesn't exist, initializes it to `by` (default 1).
    """
    settings = get_settings()
    by = body.by if body else 1

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        # Initialize with `by`
        try:
            encrypted = encrypt_value(settings.kv_secret, by)
        except KVEncryptionError as e:
            logger.error("Failed to encrypt KV value: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to encrypt value",
            )

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVIncrResponse(key=key, value=by)

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, (int, float)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not numeric",
        )

    new_value = int(value + by)

    try:
        kv.value_encrypted = encrypt_value(settings.kv_secret, new_value)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

    await session.flush()
    return KVIncrResponse(key=key, value=new_value)


@router.post("/{key}/decr")
async def decrement(
    key: str,
    body: KVIncrRequest | None = None,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVIncrResponse:
    """Atomically decrement a numeric value.

    If the key doesn't exist, initializes it to `-by` (default -1).
    """
    settings = get_settings()
    by = body.by if body else 1

    kv = await _get_kv_row_for_update(session, automation_id, key)

    if kv is None:
        # Initialize with `-by`
        try:
            encrypted = encrypt_value(settings.kv_secret, -by)
        except KVEncryptionError as e:
            logger.error("Failed to encrypt KV value: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to encrypt value",
            )

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVIncrResponse(key=key, value=-by)

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, (int, float)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not numeric",
        )

    new_value = int(value - by)

    try:
        kv.value_encrypted = encrypt_value(settings.kv_secret, new_value)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

    await session.flush()
    return KVIncrResponse(key=key, value=new_value)


@router.post("/{key}/lpush")
async def lpush(
    key: str,
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
        try:
            encrypted = encrypt_value(settings.kv_secret, value)
        except KVEncryptionError as e:
            logger.error("Failed to encrypt KV value: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to encrypt value",
            )

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVListLengthResponse(key=key, length=1)

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not a list",
        )

    value.insert(0, body.value)

    try:
        kv.value_encrypted = encrypt_value(settings.kv_secret, value)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

    await session.flush()
    return KVListLengthResponse(key=key, length=len(value))


@router.post("/{key}/rpush")
async def rpush(
    key: str,
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
        try:
            encrypted = encrypt_value(settings.kv_secret, value)
        except KVEncryptionError as e:
            logger.error("Failed to encrypt KV value: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to encrypt value",
            )

        kv = AutomationKV(
            automation_id=automation_id,
            key=key,
            value_encrypted=encrypted,
        )
        session.add(kv)
        await session.flush()
        return KVListLengthResponse(key=key, length=1)

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not a list",
        )

    value.append(body.value)

    try:
        kv.value_encrypted = encrypt_value(settings.kv_secret, value)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

    await session.flush()
    return KVListLengthResponse(key=key, length=len(value))


@router.post("/{key}/lpop")
async def lpop(
    key: str,
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

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not a list",
        )

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop(0)

    try:
        kv.value_encrypted = encrypt_value(settings.kv_secret, value)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

    await session.flush()
    return KVKeyResponse(key=key, value=popped)


@router.post("/{key}/rpop")
async def rpop(
    key: str,
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

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not a list",
        )

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop()

    try:
        kv.value_encrypted = encrypt_value(settings.kv_secret, value)
    except KVEncryptionError as e:
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )

    await session.flush()
    return KVKeyResponse(key=key, value=popped)


@router.get("/{key}/len")
async def list_length(
    key: str,
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

    try:
        value = decrypt_value(settings.kv_secret, kv.value_encrypted)
    except KVEncryptionError as e:
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )

    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not a list",
        )

    return KVListLengthResponse(key=key, length=len(value))

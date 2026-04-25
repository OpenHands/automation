"""FastAPI router for the automation KV store API.

Provides a Redis-like key-value store scoped per-automation for state persistence.
All values are encrypted at the application level using AES-256-GCM.
Authentication is via per-run JWT tokens (AUTOMATION_KV_TOKEN).

Single-Document Backend Design
==============================

While the API presents a multi-key interface (GET /kv/{key}, PUT /kv/{key}, etc.),
the backend stores all state in a SINGLE encrypted JSON document per automation.

    API "keys" → top-level fields in the state document

Example:
    PUT /kv/config   → state["config"] = value
    PUT /kv/counter  → state["counter"] = value
    GET /kv/config   → return state["config"]

This design eliminates deadlock risk:
- Only ONE row per automation to lock
- All operations serialize through that single lock
- No multi-key ordering issues possible

Trade-off: Every operation reads/writes the entire state blob. This is acceptable
because automation state is intended to be small and access is infrequent.
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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import get_settings
from automation.db import get_session
from automation.kv_helpers import (
    get_nested_value,
    require_dict,
    require_int,
    require_list,
    safe_decrypt,
    safe_encrypt,
    set_nested_value,
    validate_key,
)
from automation.kv_schemas import (
    KVBatchOperation,
    KVBatchRequest,
    KVBatchResponse,
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


def _check_state_size(state: dict[str, Any], settings=None) -> None:
    """Validate that the entire state document doesn't exceed the configured size limit.

    Args:
        state: The state dict to check (will be JSON-serialized to measure size)
        settings: Optional settings object (fetched if not provided)

    Raises:
        HTTPException: 413 Payload Too Large if state exceeds limit
    """
    import json

    if settings is None:
        settings = get_settings()

    max_size = settings.kv_max_value_size
    if max_size <= 0:
        return  # Size limit disabled

    # Measure the JSON-serialized size (this is what gets encrypted/stored)
    try:
        serialized = json.dumps(state)
    except (TypeError, ValueError):
        # If we can't serialize it, the encrypt step will fail anyway
        return

    actual_size = len(serialized.encode("utf-8"))
    if actual_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"State size ({actual_size} bytes) exceeds limit ({max_size} bytes)",
        )


# --- Database Helpers ---


async def _get_state_row(
    session: AsyncSession,
    automation_id: uuid.UUID,
) -> AutomationKV | None:
    """Get the state row for an automation (no lock)."""
    result = await session.execute(
        select(AutomationKV).where(AutomationKV.automation_id == automation_id)
    )
    return result.scalars().first()


async def _get_state_row_for_update(
    session: AsyncSession,
    automation_id: uuid.UUID,
) -> AutomationKV | None:
    """Get the state row with FOR UPDATE lock and bounded wait time.

    Since there's only ONE row per automation, this is the single lock point.
    All concurrent operations on this automation's state will serialize here.

    Lock Timeout (Deadlock Prevention):
    We set a 5-second lock_timeout before acquiring the row lock. Without this,
    a slow or stuck transaction (e.g., network issue during commit, slow crypto)
    would cause all subsequent KV operations on this automation to queue
    indefinitely. With enough concurrent requests, this exhausts the connection
    pool and degrades the entire service—not just this automation.

    SET LOCAL scopes the timeout to this transaction only, so it doesn't affect
    other queries in this session or pollute the connection pool.

    If the lock times out, PostgreSQL raises an error which we catch and convert
    to HTTP 409 Conflict, allowing clients to retry with backoff.
    """
    # Bound how long we'll wait for the row lock. This prevents a single slow
    # transaction from cascading into connection pool exhaustion.
    await session.execute(text("SET LOCAL lock_timeout = '5000ms'"))

    result = await session.execute(
        select(AutomationKV)
        .where(AutomationKV.automation_id == automation_id)
        .with_for_update()
    )
    return result.scalars().first()


def _is_lock_timeout_error(exc: Exception) -> bool:
    """Check if an exception is a PostgreSQL lock timeout error.

    PostgreSQL raises error code 55P03 (lock_not_available) when lock_timeout
    is exceeded. This can surface through asyncpg or SQLAlchemy wrappers.
    """
    error_str = str(exc).lower()
    # asyncpg surfaces this as "lock_not_available" or error code 55P03
    return (
        "lock_not_available" in error_str
        or "55p03" in error_str
        or "could not obtain lock" in error_str
        or "canceling statement due to lock timeout" in error_str
    )


def _raise_lock_conflict() -> None:
    """Raise HTTP 409 for lock timeout - signals client should retry."""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="kv_store_busy: another operation is in progress, please retry",
    )


def _decrypt_state(secret: str, row: AutomationKV | None) -> dict[str, Any]:
    """Decrypt the state document from a row, returning empty dict if no row."""
    if row is None:
        return {}
    return safe_decrypt(secret, row.state_encrypted)


async def _save_state(
    session: AsyncSession,
    automation_id: uuid.UUID,
    state: dict[str, Any],
    secret: str,
    existing_row: AutomationKV | None,
    *,
    bump_version: bool = True,
) -> AutomationKV:
    """Save the state document, creating or updating the row as needed.

    Args:
        session: Database session
        automation_id: The automation's UUID
        state: The state dict to save (will be encrypted)
        secret: Encryption secret
        existing_row: Existing row to update, or None to create new
        bump_version: If True (default), auto-increment $version

    Returns:
        The saved/updated AutomationKV row
    """
    # Auto-increment $version on every write (unless explicitly disabled)
    if bump_version:
        state["$version"] = state.get("$version", 0) + 1

    encrypted = safe_encrypt(secret, state)

    if existing_row is None:
        # Create new row
        row = AutomationKV(
            automation_id=automation_id,
            state_encrypted=encrypted,
        )
        session.add(row)
    else:
        # Update existing row
        existing_row.state_encrypted = encrypted
        row = existing_row

    await session.flush()
    await session.refresh(row)
    return row


def _get_version(state: dict[str, Any]) -> int:
    """Get the current $version from state, defaulting to 0."""
    return state.get("$version", 0)


# --- Endpoints ---


@router.get("")
async def list_keys(
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVListKeysResponse:
    """List all keys for this automation.

    Note: System keys (starting with $) are filtered from the response.
    """
    settings = get_settings()

    row = await _get_state_row(session, automation_id)
    state = _decrypt_state(settings.kv_secret, row)

    # Filter out system keys (e.g., $version)
    keys = [k for k in state.keys() if not k.startswith("$")]
    return KVListKeysResponse(keys=keys, count=len(keys))


@router.get("/{key}")
async def get_value(
    key: ValidatedKey,
    path: str | None = Query(default=None, description="Nested path (dot notation)"),
    meta: bool = Query(default=False, description="Include metadata and version"),
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse | KVKeyPathResponse | KVKeyMetaResponse:
    """Get a value by key, optionally at a nested path.

    With meta=true, includes version for optimistic concurrency control.
    """
    settings = get_settings()

    row = await _get_state_row(session, automation_id)
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = state[key]

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
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="key_not_found",
            )
        return KVKeyMetaResponse(
            key=key,
            value=value,
            version=_get_version(state),
            created_at=row.created_at.isoformat(),
            updated_at=row.updated_at.isoformat(),
        )

    return KVKeyResponse(key=key, value=value)


@router.put("/{key}")
async def set_value(
    key: ValidatedKey,
    body: Annotated[Any, Body()],  # Accept any JSON body directly as the value
    response: Response,
    nx: bool = Query(default=False, description="Only set if key does not exist"),
    xx: bool = Query(default=False, description="Only set if key exists"),
    if_version: int | None = Query(
        default=None,
        description="Only set if current state version matches (optimistic lock)",
    ),
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVSetResponse | KVConflictResponse:
    """Set a value for a key.

    The entire request body is stored as the value.

    Query params:
    - nx=true: Only set if key does NOT exist (like Redis SETNX)
    - xx=true: Only set if key DOES exist
    - if_version=N: Only set if current $version equals N (optimistic concurrency)

    Returns:
    - 200: Key updated (existing key)
    - 201: Key created (new key, or nx=true success)
    - 409: Conflict (nx/xx/if_version check failed)
    - 413: Payload too large (state exceeds size limit)
    """
    settings = get_settings()

    if nx and xx:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot use both nx and xx",
        )

    # Lock the state row for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    # Check version if specified (optimistic concurrency)
    if if_version is not None:
        current_version = _get_version(state)
        if current_version != if_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "version_mismatch",
                    "expected_version": if_version,
                    "actual_version": current_version,
                },
            )

    key_exists = key in state

    if nx and key_exists:
        response.status_code = status.HTTP_409_CONFLICT
        return KVConflictResponse(key=key, created=False, error="key_exists")

    if xx and not key_exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="key_not_exists",
        )

    # Update state
    state[key] = body
    _check_state_size(state, settings)

    # Save
    saved_row = await _save_state(
        session, automation_id, state, settings.kv_secret, row
    )

    created = not key_exists
    if created:
        response.status_code = status.HTTP_201_CREATED

    return KVSetResponse(
        key=key,
        value=body,
        created=created,
        updated_at=saved_row.updated_at.isoformat(),
    )


@router.patch("/{key}")
async def patch_value(
    key: ValidatedKey,
    body: KVPatchRequest,
    if_version: int | None = Query(
        default=None,
        description="Only patch if current state version matches (optimistic lock)",
    ),
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyPathResponse:
    """Update a nested path within an existing value.

    Query params:
    - if_version=N: Only patch if current $version equals N (optimistic concurrency)
    """
    settings = get_settings()

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    # Check version if specified (optimistic concurrency)
    if if_version is not None:
        current_version = _get_version(state)
        if current_version != if_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "version_mismatch",
                    "expected_version": if_version,
                    "actual_version": current_version,
                },
            )

    if key not in state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = state[key]
    require_dict(value)

    try:
        set_nested_value(value, body.path, body.value)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid_path: {e}",
        )

    state[key] = value
    _check_state_size(state, settings)

    await _save_state(session, automation_id, state, settings.kv_secret, row)

    return KVKeyPathResponse(
        key=key,
        path=body.path,
        value=body.value,
    )


def _has_user_keys(state: dict[str, Any]) -> bool:
    """Check if state has any user keys (excluding system keys like $version)."""
    return any(not k.startswith("$") for k in state.keys())


@router.delete("/{key}")
async def delete_key(
    key: ValidatedKey,
    if_version: int | None = Query(
        default=None,
        description="Only delete if current state version matches (optimistic lock)",
    ),
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVDeleteResponse:
    """Delete a key.

    Query params:
    - if_version=N: Only delete if current $version equals N (optimistic concurrency)
    """
    settings = get_settings()

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    # Check version if specified (optimistic concurrency)
    if if_version is not None:
        current_version = _get_version(state)
        if current_version != if_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "version_mismatch",
                    "expected_version": if_version,
                    "actual_version": current_version,
                },
            )

    if key not in state:
        return KVDeleteResponse(key=key, deleted=False)

    del state[key]

    if row is not None:
        if _has_user_keys(state):
            # Still have user keys, update the row
            await _save_state(session, automation_id, state, settings.kv_secret, row)
        else:
            # No user keys left, delete the row entirely
            await session.delete(row)
            await session.flush()

    return KVDeleteResponse(key=key, deleted=True)


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

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        # Initialize with `by`
        state[key] = by
        new_value = by
    else:
        value = state[key]
        require_int(value)
        new_value = value + by
        state[key] = new_value

    _check_state_size(state, settings)
    await _save_state(session, automation_id, state, settings.kv_secret, row)

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

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        # Initialize with `-by`
        state[key] = -by
        new_value = -by
    else:
        value = state[key]
        require_int(value)
        new_value = value - by
        state[key] = new_value

    _check_state_size(state, settings)
    await _save_state(session, automation_id, state, settings.kv_secret, row)

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

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        # Initialize with single-element list
        state[key] = [body.value]
    else:
        value = state[key]
        require_list(value)
        value.insert(0, body.value)
        state[key] = value

    _check_state_size(state, settings)
    await _save_state(session, automation_id, state, settings.kv_secret, row)

    return KVListLengthResponse(key=key, length=len(state[key]))


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

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        # Initialize with single-element list
        state[key] = [body.value]
    else:
        value = state[key]
        require_list(value)
        value.append(body.value)
        state[key] = value

    _check_state_size(state, settings)
    await _save_state(session, automation_id, state, settings.kv_secret, row)

    return KVListLengthResponse(key=key, length=len(state[key]))


@router.post("/{key}/lpop")
async def lpop(
    key: ValidatedKey,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse:
    """Pop a value from the left (front) of a list.

    Returns null if key doesn't exist or list is empty.
    """
    settings = get_settings()

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        return KVKeyResponse(key=key, value=None)

    value = state[key]
    require_list(value)

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop(0)
    state[key] = value

    await _save_state(session, automation_id, state, settings.kv_secret, row)

    return KVKeyResponse(key=key, value=popped)


@router.post("/{key}/rpop")
async def rpop(
    key: ValidatedKey,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse:
    """Pop a value from the right (back) of a list.

    Returns null if key doesn't exist or list is empty.
    """
    settings = get_settings()

    # Lock for atomic read-modify-write
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        return KVKeyResponse(key=key, value=None)

    value = state[key]
    require_list(value)

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop()
    state[key] = value

    await _save_state(session, automation_id, state, settings.kv_secret, row)

    return KVKeyResponse(key=key, value=popped)


@router.get("/{key}/len")
async def list_length(
    key: ValidatedKey,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVListLengthResponse:
    """Get the length of a list."""
    settings = get_settings()

    row = await _get_state_row(session, automation_id)
    state = _decrypt_state(settings.kv_secret, row)

    if key not in state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = state[key]
    require_list(value)

    return KVListLengthResponse(key=key, length=len(value))


# --- Batch Operations ---


class KVOperationError(Exception):
    """Raised when a batch operation fails validation."""

    pass


def _validate_batch_key(key: str) -> None:
    """Validate a key for batch operations (same rules as validate_key).

    Raises:
        KVOperationError: If key is invalid
    """
    if not key:
        raise KVOperationError("key cannot be empty")
    if not key.strip():
        raise KVOperationError("key cannot be whitespace-only")
    if key.startswith("$"):
        raise KVOperationError("keys starting with '$' are reserved for system use")
    if len(key) > 255:
        raise KVOperationError(f"key exceeds 255 chars ({len(key)} given)")


def _execute_batch_operation(
    state: dict[str, Any],
    op: KVBatchOperation,
) -> dict[str, Any]:
    """Execute a single operation within a batch.

    Args:
        state: The current state dict (modified in place)
        op: The operation to execute

    Returns:
        Result dict for this operation

    Raises:
        KVOperationError: If operation fails validation
    """
    _validate_batch_key(op.key)
    key = op.key

    if op.op == "set":
        key_existed = key in state
        # Handle nx (set if not exists)
        if op.nx and key_existed:
            raise KVOperationError(f"key '{key}' already exists (nx=true)")
        # Handle xx (set if exists)
        if op.xx and not key_existed:
            raise KVOperationError(f"key '{key}' does not exist (xx=true)")
        state[key] = op.value
        return {"op": "set", "key": key, "success": True, "created": not key_existed}

    elif op.op == "delete":
        deleted = key in state
        if deleted:
            del state[key]
        return {"op": "delete", "key": key, "success": True, "deleted": deleted}

    elif op.op == "incr":
        by = op.by
        if key not in state:
            state[key] = by
            new_value = by
        else:
            value = state[key]
            if isinstance(value, bool):
                raise KVOperationError(f"key '{key}' is boolean, not integer")
            if not isinstance(value, int):
                raise KVOperationError(f"key '{key}' is not an integer")
            new_value = value + by
            state[key] = new_value
        return {"op": "incr", "key": key, "success": True, "value": new_value}

    elif op.op == "decr":
        by = op.by
        if key not in state:
            state[key] = -by
            new_value = -by
        else:
            value = state[key]
            if isinstance(value, bool):
                raise KVOperationError(f"key '{key}' is boolean, not integer")
            if not isinstance(value, int):
                raise KVOperationError(f"key '{key}' is not an integer")
            new_value = value - by
            state[key] = new_value
        return {"op": "decr", "key": key, "success": True, "value": new_value}

    elif op.op == "lpush":
        if key not in state:
            state[key] = [op.value]
        else:
            value = state[key]
            if not isinstance(value, list):
                raise KVOperationError(f"key '{key}' is not a list")
            value.insert(0, op.value)
        return {"op": "lpush", "key": key, "success": True, "length": len(state[key])}

    elif op.op == "rpush":
        if key not in state:
            state[key] = [op.value]
        else:
            value = state[key]
            if not isinstance(value, list):
                raise KVOperationError(f"key '{key}' is not a list")
            value.append(op.value)
        return {"op": "rpush", "key": key, "success": True, "length": len(state[key])}

    elif op.op == "lpop":
        if key not in state:
            return {"op": "lpop", "key": key, "success": True, "value": None}
        value = state[key]
        if not isinstance(value, list):
            raise KVOperationError(f"key '{key}' is not a list")
        if len(value) == 0:
            return {"op": "lpop", "key": key, "success": True, "value": None}
        popped = value.pop(0)
        return {"op": "lpop", "key": key, "success": True, "value": popped}

    elif op.op == "rpop":
        if key not in state:
            return {"op": "rpop", "key": key, "success": True, "value": None}
        value = state[key]
        if not isinstance(value, list):
            raise KVOperationError(f"key '{key}' is not a list")
        if len(value) == 0:
            return {"op": "rpop", "key": key, "success": True, "value": None}
        popped = value.pop()
        return {"op": "rpop", "key": key, "success": True, "value": popped}

    elif op.op == "patch":
        if key not in state:
            state[key] = {}
        value = state[key]
        if not isinstance(value, dict):
            raise KVOperationError(f"key '{key}' is not an object")
        try:
            set_nested_value(value, op.path, op.value)
        except ValueError as e:
            raise KVOperationError(str(e))
        return {"op": "patch", "key": key, "success": True}

    else:
        raise KVOperationError(f"unknown operation: {op.op}")


@router.post("/batch")
async def batch(
    body: KVBatchRequest,
    automation_id: uuid.UUID = Depends(get_automation_id_from_token),
    session: AsyncSession = Depends(get_session),
) -> KVBatchResponse:
    """Execute multiple KV operations atomically in a single transaction.

    All operations succeed or none do. Use `if_version` for optimistic
    concurrency control - the batch will be rejected if the current state
    version doesn't match.

    Operations are executed in order. The $version is incremented once
    for the entire batch, not per operation.

    Returns:
    - 200: All operations succeeded
    - 400: An operation failed validation (e.g., incr on a list)
    - 409: Version mismatch (if_version specified but doesn't match)
    - 409: Lock timeout (another operation in progress)
    - 413: Payload too large (state exceeds size limit)
    """
    settings = get_settings()

    # Acquire lock for atomic batch execution
    try:
        row = await _get_state_row_for_update(session, automation_id)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise

    state = _decrypt_state(settings.kv_secret, row)
    current_version = _get_version(state)

    # Check version if specified
    if body.if_version is not None and current_version != body.if_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "version_mismatch",
                "message": "State was modified by another process",
                "expected_version": body.if_version,
                "actual_version": current_version,
            },
        )

    # Execute all operations
    results = []
    for i, op in enumerate(body.operations):
        try:
            result = _execute_batch_operation(state, op)
            results.append(result)
        except KVOperationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "operation_failed",
                    "message": str(e),
                    "operation_index": i,
                    "operation": {"op": op.op, "key": op.key},
                },
            )

    # Validate state size before saving
    _check_state_size(state, settings)

    # Save state (auto-increments $version)
    await _save_state(session, automation_id, state, settings.kv_secret, row)

    return KVBatchResponse(version=_get_version(state), results=results)

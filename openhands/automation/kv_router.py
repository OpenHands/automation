"""FastAPI router for the automation KV store API.

Provides a Redis-like key-value store scoped per-automation for state persistence.
Values are encrypted at the application level via the SDK's :class:`Cipher`
helper (Fernet: AES-128-CBC + HMAC-SHA256) before storage. Authentication is
via per-run JWT tokens (AUTOMATION_KV_TOKEN).

Per-Key Backend Design
======================

The API presents independent keys and the backend stores them that way: one
encrypted row per ``(automation_id, key)`` pair in ``automation_kv`` plus a
small per-automation metadata row in ``automation_kv_meta`` that holds the
single global ``$version`` counter.

    PUT /kv/config   → row (automation_id, "config") = encrypt(value)
    PUT /kv/counter  → row (automation_id, "counter") = encrypt(value)
    GET /kv/config   → decrypt row (automation_id, "config")

Each value is therefore limited by ``kv_max_value_size`` on its own; a large
number of small keys never trips the limit.

Concurrency and atomicity:
- Every write locks the automation's metadata row first, then (for multi-key
  batch operations) the affected key rows in sorted-key order. The metadata
  row is the single serialization point for all writers of one automation, and
  the sorted key order makes multi-key acquisition deterministic, so batch
  writes stay all-or-nothing and deadlock-safe across replicas.
- ``$version`` is global per automation, preserving existing ``if_version``
  optimistic concurrency semantics across all keys.
"""

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import (
    AuthenticatedUser,
    authenticate_request,
    get_http_client,
)
from openhands.automation.config import KVSettings, get_config
from openhands.automation.db import get_session, using_sqlite
from openhands.automation.kv_helpers import (
    get_nested_value,
    require_dict,
    require_int,
    require_list,
    safe_decrypt,
    safe_encrypt,
    set_nested_value,
    validate_key,
)
from openhands.automation.kv_metrics import (
    record_conflict,
    record_lock_wait,
    record_state_size,
)
from openhands.automation.kv_schemas import (
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
from openhands.automation.models import Automation, AutomationKV, AutomationKVMeta
from openhands.automation.utils.kv import KVTokenClaims, KVTokenError, verify_kv_token


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kv", tags=["KV Store"])

# Default and maximum page sizes for GET /v1/kv.
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000


# --- Authentication ---


@dataclass
class KVAuthContext:
    """Resolved authorization for a KV request.

    Holds the automation_id whose state should be accessed.  The automation_id
    comes from either:

    1. A per-run KV JWT token (``Authorization: Bearer <jwt>``) — used by
       automation scripts during a run.  The token embeds the automation_id
       as a trusted claim.

    2. User authentication (API key / X-Session-API-Key / cookie) — used by
       humans and spawned conversations acting on a user's behalf.  Requires
       an ``automation_id`` query parameter; the user must be a member of the
       automation's org.
    """

    automation_id: uuid.UUID
    auth_method: str  # "kv_token" | "user"


async def _try_kv_token_auth(
    authorization: str,
    kv_secret: str,
) -> KVTokenClaims | None:
    """Attempt to authenticate via KV JWT token.

    Returns None if the Authorization header is not a valid KV JWT (allowing
    the caller to fall through to user auth).  Raises HTTPException for
    malformed tokens that look like KV JWTs but fail verification.
    """
    if not authorization.startswith("Bearer "):
        return None

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None

    # A KV JWT has exactly three dot-separated base64 segments.  A user API
    # key will not match this shape, so we can cheaply distinguish the two
    # and avoid a spurious "Invalid token" error for legitimate API keys.
    if token.count(".") != 2:
        return None

    try:
        return verify_kv_token(kv_secret, token)
    except KVTokenError:
        # The token *looks* like a JWT but failed verification — this is a
        # real auth failure, not a "try user auth" situation.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired KV token",
        )


async def _verify_automation_access(
    session: AsyncSession,
    user: AuthenticatedUser,
    automation_id: uuid.UUID,
) -> None:
    """Verify that the user has access to the given automation's KV data.

    The user must be a member of the automation's org (view_automations).
    Unlike management endpoints, we do not require manage_automations for KV
    reads — a member who can see the automation can inspect its state.
    """
    result = await session.execute(
        select(Automation).where(Automation.id == automation_id)
    )
    automation = result.scalars().first()
    if automation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation not found",
        )
    if automation.org_id != user.org_id:
        # Don't leak existence — return 404 rather than 403
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automation not found",
        )


async def _try_resolve_user(
    request: Request,
    http_client: Any = Depends(get_http_client),
) -> AuthenticatedUser | None:
    """Attempt to resolve user auth; return None if it cannot be resolved.

    This wrapper lets ``get_kv_auth_context`` declare user auth as an optional
    FastAPI dependency (so it can be overridden in tests) while still trying
    the KV token path first.  Returns None on 401 (no/invalid credential) or
    on unexpected errors (e.g. no http_client available when KV token auth
    is being used).  Other HTTP errors (429, 502) propagate so the client
    sees them rather than silently falling through.
    """
    try:
        return await authenticate_request(request, http_client)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise
    except Exception as exc:
        # If user auth fails for non-HTTP reasons (e.g. no http_client
        # available when the caller is using KV token auth), fall through
        # to the KV token path rather than crashing.
        logger.debug("User auth skipped due to error: %s", exc)
        return None


async def get_kv_auth_context(
    authorization: Annotated[str | None, Header()] = None,
    user: Annotated[AuthenticatedUser | None, Depends(_try_resolve_user)] = None,
    automation_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Automation ID (required for user auth, ignored for KV token auth)"
            )
        ),
    ] = None,
    session: AsyncSession = Depends(get_session),
) -> KVAuthContext:
    """Unified auth dependency for KV endpoints.

    Supports two authentication paths:

    1. **KV JWT token** (automation runs): ``Authorization: Bearer <jwt>``
       where the JWT is signed with ``AUTOMATION_KV_SECRET`` and contains the
       automation_id as a claim.  No query parameter needed.  User auth is
       NOT required in this path — the KV token is self-contained.

    2. **User auth** (humans / spawned conversations): Standard OpenHands
       authentication (API key, X-Session-API-Key, or cookie).  Requires an
       ``automation_id`` query parameter; the user must be in the automation's
       org.

    The KV JWT path is checked first so existing automation scripts continue
    to work without changes.  If the Bearer token is not a JWT (or is absent),
    user auth is used.
    """
    kv_config = get_config().kv

    if not kv_config.kv_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="KV store not configured (missing AUTOMATION_KV_SECRET)",
        )

    # --- Path 1: KV JWT token ---
    if authorization:
        claims = await _try_kv_token_auth(authorization, kv_config.kv_secret)
        if claims is not None:
            return KVAuthContext(
                automation_id=claims.automation_id,
                auth_method="kv_token",
            )

    # --- Path 2: User auth ---
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: provide a KV token or user credentials",
        )

    if automation_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "automation_id query parameter is required "
                "for user-authenticated KV access"
            ),
        )

    await _verify_automation_access(session, user, automation_id)

    return KVAuthContext(
        automation_id=automation_id,
        auth_method="user",
    )


# Backward-compatible alias — tests override this to bypass auth.
# New code should depend on get_kv_auth_context instead.
async def get_token_claims(
    authorization: Annotated[str, Header()],
) -> KVTokenClaims:
    """Extract and verify claims from the KV token (legacy).

    Kept for backward compatibility with tests that override this dependency
    directly.  Production endpoints now use :func:`get_kv_auth_context`.
    """
    kv_config = get_config().kv

    if not kv_config.kv_secret:
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
        return verify_kv_token(kv_config.kv_secret, token)
    except KVTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


# --- Validation Helpers ---


# Type alias for validated KV keys - ensures key validation is applied
# Use this as a FastAPI path parameter annotation: key: ValidatedKey
ValidatedKey = Annotated[str, Depends(lambda key: validate_key(key))]


def _check_value_size(
    key: str, value: Any, kv_config: KVSettings | None = None
) -> None:
    """Validate that a single value doesn't exceed the configured size limit.

    Only the value being written is measured, never the combined size of every
    key an automation owns. A large number of small keys is a normal workload
    and must not fail here.

    Args:
        key: The key being written (for the error body)
        value: The value to check (JSON-serialized to measure size)
        kv_config: Optional KVSettings object (fetched if not provided)

    Raises:
        HTTPException: 413 Payload Too Large if the value exceeds the limit
    """
    if kv_config is None:
        kv_config = get_config().kv

    max_size = kv_config.kv_max_value_size
    if max_size <= 0:
        return  # Size limit disabled

    # Measure the JSON-serialized size (this is what gets encrypted/stored)
    try:
        serialized = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        # If we can't serialize it, the encrypt step will fail anyway
        return

    actual_size = len(serialized.encode("utf-8"))
    if actual_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "value_too_large",
                "message": (
                    f"Value for key '{key}' is {actual_size} bytes, "
                    f"exceeding the {max_size} byte per-value limit"
                ),
                "key": key,
                "size": actual_size,
                "limit": max_size,
            },
        )


def _check_batch_value_sizes(
    state: dict[str, Any], kv_config: KVSettings | None = None
) -> None:
    """Check the per-value size limit for every key in a batch result."""
    for key, value in state.items():
        _check_value_size(key, value, kv_config)


# --- Database Helpers ---


def _serialize_for_storage(value: Any, secret: str) -> str:
    """Encrypt a single value and record its stored size."""
    encrypted = safe_encrypt(secret, value)
    record_state_size(len(encrypted))
    return encrypted


async def _apply_lock_timeouts(session: AsyncSession, lock_timeout_ms: int) -> None:
    """Set PostgreSQL statement and lock timeouts for this transaction.

    Statement timeout (2x lock timeout) is a safety net for runaway operations
    after a lock is held; lock timeout fails fast while waiting for a lock.
    SET LOCAL scopes both to this transaction, so they don't leak to other
    queries on the connection. Both are PostgreSQL-only.
    """
    if using_sqlite():
        return
    statement_timeout_ms = lock_timeout_ms * 2
    await session.execute(
        text(f"SET LOCAL statement_timeout = '{statement_timeout_ms}ms'")
    )
    await session.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout_ms}ms'"))


async def _lock_meta_row(
    session: AsyncSession,
    automation_id: uuid.UUID,
    lock_timeout_ms: int,
) -> AutomationKVMeta | None:
    """Apply lock timeouts and lock the metadata row (None if not yet created)."""
    await _apply_lock_timeouts(session, lock_timeout_ms)
    query = select(AutomationKVMeta).where(
        AutomationKVMeta.automation_id == automation_id
    )
    if not using_sqlite():
        query = query.with_for_update()
    with record_lock_wait():
        result = await session.execute(query)
    return result.scalars().first()


async def _ensure_meta_row(
    session: AsyncSession,
    automation_id: uuid.UUID,
    lock_timeout_ms: int,
) -> AutomationKVMeta:
    """Lock the automation's metadata row, creating it if necessary.

    The metadata row is the single serialization point for all writers of one
    automation: every write path locks it first, so the creation below can only
    race another first-writer, not an in-flight update.

    Returns:
        The locked (and possibly newly created) metadata row.
    """
    meta = await _lock_meta_row(session, automation_id, lock_timeout_ms)
    if meta is not None:
        return meta

    meta = AutomationKVMeta(automation_id=automation_id, version=0)
    # Creation needs a savepoint: if a concurrent transaction inserted the row
    # between our SELECT and INSERT, only this INSERT must be discarded, not the
    # whole transaction (which may already hold other work).
    try:
        async with session.begin_nested():
            session.add(meta)
            await session.flush()
    except IntegrityError:
        meta = await _lock_meta_row(session, automation_id, lock_timeout_ms)
        if meta is None:
            raise
    return meta


async def _lock_key_rows(
    session: AsyncSession,
    automation_id: uuid.UUID,
    keys: Iterable[str],
) -> dict[str, AutomationKV]:
    """Lock the given key rows in sorted-key order and return them by key.

    Sorted order makes multi-key acquisition deterministic across concurrent
    transactions, so two batches touching overlapping keys cannot deadlock.
    """
    ordered = sorted(set(keys))
    if not ordered:
        return {}
    query = (
        select(AutomationKV)
        .where(
            AutomationKV.automation_id == automation_id,
            AutomationKV.key.in_(ordered),
        )
        .order_by(AutomationKV.key)
    )
    if not using_sqlite():
        query = query.with_for_update()
    result = await session.execute(query)
    rows = result.scalars().all()
    return {row.key: row for row in rows}


async def _upsert_key_rows(
    session: AsyncSession,
    automation_id: uuid.UUID,
    state: dict[str, Any],
    secret: str,
    existing_rows: dict[str, AutomationKV],
) -> dict[str, AutomationKV]:
    """Encrypt and write/update/delete key rows to match ``state``.

    ``state`` is the desired user-key state after the operation; keys absent
    from ``existing_rows`` are inserted, present keys updated, and existing rows
    whose key is missing from ``state`` are deleted. Rows are updated in sorted
    order for the same deterministic ordering as the locks that were taken.

    Returns:
        Mapping of key -> row for every key in ``state``.
    """
    result: dict[str, AutomationKV] = {}
    for key in sorted(state):
        encrypted = _serialize_for_storage(state[key], secret)
        row = existing_rows.get(key)
        if row is None:
            row = AutomationKV(
                automation_id=automation_id,
                key=key,
                value_encrypted=encrypted,
            )
            session.add(row)
        else:
            row.value_encrypted = encrypted
        result[key] = row

    for key in sorted(set(existing_rows) - set(state)):
        await session.delete(existing_rows[key])

    return result


async def _bump_version(
    session: AsyncSession,
    meta: AutomationKVMeta,
) -> int:
    """Increment the automation's global version and return the new value."""
    meta.version = int(meta.version or 0) + 1
    session.add(meta)
    await session.flush()
    return meta.version


def _is_lock_timeout_error(exc: Exception) -> bool:
    """Check if an exception is a PostgreSQL lock or statement timeout error.

    PostgreSQL error codes:
    - 55P03 (lock_not_available): lock_timeout exceeded while waiting for lock
    - 57014 (query_canceled): statement_timeout exceeded during query execution

    Both indicate the operation took too long and should be retried.
    """
    error_str = str(exc).lower()
    return (
        # Lock timeout errors (55P03)
        "lock_not_available" in error_str
        or "55p03" in error_str
        or "could not obtain lock" in error_str
        or "canceling statement due to lock timeout" in error_str
        # Statement timeout errors (57014)
        or "query_canceled" in error_str
        or "57014" in error_str
        or "canceling statement due to statement timeout" in error_str
    )


# Default retry delay in seconds for 409 responses
_RETRY_AFTER_SECONDS = "1"


def _raise_lock_conflict() -> None:
    """Raise HTTP 409 for lock/statement timeout - signals client should retry.

    Includes Retry-After header suggesting initial backoff delay.
    Clients should use exponential backoff with jitter on subsequent retries.
    """
    record_conflict("lock_timeout")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="kv_store_busy: another operation is in progress, please retry",
        headers={"Retry-After": _RETRY_AFTER_SECONDS},
    )


def _raise_version_conflict(expected: int, actual: int) -> None:
    """Raise HTTP 409 for version mismatch - signals optimistic concurrency failure."""
    record_conflict("version_mismatch")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "version_mismatch",
            "message": "State was modified by another process",
            "expected_version": expected,
            "actual_version": actual,
        },
        headers={"Retry-After": _RETRY_AFTER_SECONDS},
    )


# --- Database Helpers (endpoints) ---


async def _lock_write(
    session: AsyncSession,
    automation_id: uuid.UUID,
    keys: Iterable[str],
    lock_timeout_ms: int,
) -> tuple[AutomationKVMeta, dict[str, AutomationKV]]:
    """Lock the metadata row and the given key rows for a write.

    The metadata row is locked first so all writers for an automation serialize
    on one lock; key rows are then locked in sorted order by ``_lock_key_rows``.
    Lock/statement timeouts are surfaced as HTTP 409 by the caller.
    """
    try:
        meta = await _ensure_meta_row(session, automation_id, lock_timeout_ms)
        rows = await _lock_key_rows(session, automation_id, keys)
    except Exception as e:
        if _is_lock_timeout_error(e):
            _raise_lock_conflict()
        raise
    return meta, rows


def _decrypt_value(secret: str, row: AutomationKV) -> Any:
    """Decrypt a single key row's value."""
    return safe_decrypt(secret, row.value_encrypted)


async def _lock_existing_key(
    session: AsyncSession,
    automation_id: uuid.UUID,
    key: str,
    lock_timeout_ms: int,
) -> tuple[AutomationKVMeta, AutomationKV | None]:
    """Lock metadata + one key row for a read-modify-write on a single key."""
    meta, rows = await _lock_write(session, automation_id, [key], lock_timeout_ms)
    return meta, rows.get(key)


# --- Endpoints ---


@router.get("")
async def list_keys(
    limit: int = Query(
        default=DEFAULT_LIST_LIMIT,
        ge=1,
        le=MAX_LIST_LIMIT,
        description="Maximum number of keys to return (paginated)",
    ),
    offset: int = Query(default=0, ge=0, description="Number of keys to skip"),
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVListKeysResponse:
    """List keys for this automation, paginated.

    There is no per-automation key-count quota; keys can grow with database
    capacity, so callers page through them with ``limit``/``offset``. ``total``
    is the full key count and ``count`` is the size of this page.

    Note: System keys (starting with $) are not stored as rows and never
    appear here.
    """
    rows = await session.execute(
        select(AutomationKV.key)
        .where(AutomationKV.automation_id == ctx.automation_id)
        .order_by(AutomationKV.key)
        .limit(limit)
        .offset(offset)
    )
    keys = [row[0] for row in rows]

    total_result = await session.execute(
        select(func.count())
        .select_from(AutomationKV)
        .where(AutomationKV.automation_id == ctx.automation_id)
    )
    total = int(total_result.scalar_one())

    return KVListKeysResponse(
        keys=keys,
        count=len(keys),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{key}")
async def get_value(
    key: ValidatedKey,
    path: str | None = Query(default=None, description="Nested path (dot notation)"),
    meta: bool = Query(default=False, description="Include metadata and version"),
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse | KVKeyPathResponse | KVKeyMetaResponse:
    """Get a value by key, optionally at a nested path.

    With meta=true, includes version for optimistic concurrency control.
    """
    kv_config = get_config().kv

    result = await session.execute(
        select(AutomationKV, AutomationKVMeta.version)
        .outerjoin(
            AutomationKVMeta,
            AutomationKVMeta.automation_id == AutomationKV.automation_id,
        )
        .where(
            AutomationKV.automation_id == ctx.automation_id,
            AutomationKV.key == key,
        )
    )
    record = result.first()
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )
    row, version = record

    value = _decrypt_value(kv_config.kv_secret, row)

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
            version=0 if version is None else int(version),
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
    ctx: KVAuthContext = Depends(get_kv_auth_context),
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
    - 413: Payload too large (this value exceeds the per-value size limit)
    """
    kv_config = get_config().kv

    if nx and xx:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot use both nx and xx",
        )

    # Reject an oversized value before touching the database.
    _check_value_size(key, body, kv_config)

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    # Check version if specified (optimistic concurrency)
    if if_version is not None and int(meta.version) != if_version:
        _raise_version_conflict(if_version, int(meta.version))

    key_exists = existing is not None

    if nx and key_exists:
        response.status_code = status.HTTP_409_CONFLICT
        return KVConflictResponse(key=key, created=False, error="key_exists")

    if xx and not key_exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="key_not_exists",
        )

    rows = {key: existing} if existing is not None else {}
    saved_rows = await _upsert_key_rows(
        session, ctx.automation_id, {key: body}, kv_config.kv_secret, rows
    )
    await _bump_version(session, meta)
    # Ensure server-set timestamps are loaded before reading updated_at.
    await session.refresh(saved_rows[key])

    created = not key_exists
    if created:
        response.status_code = status.HTTP_201_CREATED

    return KVSetResponse(
        key=key,
        value=body,
        created=created,
        updated_at=saved_rows[key].updated_at.isoformat(),
    )


@router.patch("/{key}")
async def patch_value(
    key: ValidatedKey,
    body: KVPatchRequest,
    if_version: int | None = Query(
        default=None,
        description="Only patch if current state version matches (optimistic lock)",
    ),
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVKeyPathResponse:
    """Update a nested path within an existing value.

    Query params:
    - if_version=N: Only patch if current $version equals N (optimistic concurrency)
    """
    kv_config = get_config().kv

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if if_version is not None and int(meta.version) != if_version:
        _raise_version_conflict(if_version, int(meta.version))

    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = _decrypt_value(kv_config.kv_secret, existing)
    require_dict(value)

    try:
        set_nested_value(value, body.path, body.value)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid_path: {e}",
        )

    _check_value_size(key, value, kv_config)
    await _upsert_key_rows(
        session, ctx.automation_id, {key: value}, kv_config.kv_secret, {key: existing}
    )
    await _bump_version(session, meta)

    return KVKeyPathResponse(
        key=key,
        path=body.path,
        value=body.value,
    )


@router.delete("/{key}")
async def delete_key(
    key: ValidatedKey,
    if_version: int | None = Query(
        default=None,
        description="Only delete if current state version matches (optimistic lock)",
    ),
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVDeleteResponse:
    """Delete a key.

    Query params:
    - if_version=N: Only delete if current $version equals N (optimistic concurrency)
    """
    kv_config = get_config().kv

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if if_version is not None and int(meta.version) != if_version:
        _raise_version_conflict(if_version, int(meta.version))

    if existing is None:
        return KVDeleteResponse(key=key, deleted=False)

    await _upsert_key_rows(
        session, ctx.automation_id, {}, kv_config.kv_secret, {key: existing}
    )
    await _bump_version(session, meta)

    return KVDeleteResponse(key=key, deleted=True)


@router.post("/{key}/incr")
async def increment(
    key: ValidatedKey,
    body: KVIncrRequest | None = None,
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVIncrResponse:
    """Atomically increment an integer value.

    If the key doesn't exist, initializes it to `by` (default 1).

    Note: The stored value must be an integer. Float values are rejected
    because integer arithmetic on floats can cause precision loss.
    """
    kv_config = get_config().kv
    by = body.by if body else 1

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if existing is None:
        new_value = by
    else:
        value = _decrypt_value(kv_config.kv_secret, existing)
        require_int(value)
        new_value = value + by

    _check_value_size(key, new_value, kv_config)
    await _upsert_key_rows(
        session,
        ctx.automation_id,
        {key: new_value},
        kv_config.kv_secret,
        {key: existing} if existing is not None else {},
    )
    await _bump_version(session, meta)

    return KVIncrResponse(key=key, value=new_value)


@router.post("/{key}/decr")
async def decrement(
    key: ValidatedKey,
    body: KVIncrRequest | None = None,
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVIncrResponse:
    """Atomically decrement an integer value.

    If the key doesn't exist, initializes it to `-by` (default -1).

    Note: The stored value must be an integer. Float values are rejected
    because integer arithmetic on floats can cause precision loss.
    """
    kv_config = get_config().kv
    by = body.by if body else 1

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if existing is None:
        new_value = -by
    else:
        value = _decrypt_value(kv_config.kv_secret, existing)
        require_int(value)
        new_value = value - by

    _check_value_size(key, new_value, kv_config)
    await _upsert_key_rows(
        session,
        ctx.automation_id,
        {key: new_value},
        kv_config.kv_secret,
        {key: existing} if existing is not None else {},
    )
    await _bump_version(session, meta)

    return KVIncrResponse(key=key, value=new_value)


@router.post("/{key}/lpush")
async def lpush(
    key: ValidatedKey,
    body: KVListPushRequest,
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVListLengthResponse:
    """Push a value to the left (front) of a list.

    Creates the list if it doesn't exist.
    """
    kv_config = get_config().kv

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if existing is None:
        value: list[Any] = [body.value]
    else:
        value = _decrypt_value(kv_config.kv_secret, existing)
        require_list(value)
        value.insert(0, body.value)

    _check_value_size(key, value, kv_config)
    await _upsert_key_rows(
        session,
        ctx.automation_id,
        {key: value},
        kv_config.kv_secret,
        {key: existing} if existing is not None else {},
    )
    await _bump_version(session, meta)

    return KVListLengthResponse(key=key, length=len(value))


@router.post("/{key}/rpush")
async def rpush(
    key: ValidatedKey,
    body: KVListPushRequest,
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVListLengthResponse:
    """Push a value to the right (back) of a list.

    Creates the list if it doesn't exist.
    """
    kv_config = get_config().kv

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if existing is None:
        value: list[Any] = [body.value]
    else:
        value = _decrypt_value(kv_config.kv_secret, existing)
        require_list(value)
        value.append(body.value)

    _check_value_size(key, value, kv_config)
    await _upsert_key_rows(
        session,
        ctx.automation_id,
        {key: value},
        kv_config.kv_secret,
        {key: existing} if existing is not None else {},
    )
    await _bump_version(session, meta)

    return KVListLengthResponse(key=key, length=len(value))


@router.post("/{key}/lpop")
async def lpop(
    key: ValidatedKey,
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse:
    """Pop a value from the left (front) of a list.

    Returns null if key doesn't exist or list is empty.
    """
    kv_config = get_config().kv

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if existing is None:
        return KVKeyResponse(key=key, value=None)

    value = _decrypt_value(kv_config.kv_secret, existing)
    require_list(value)

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop(0)
    await _upsert_key_rows(
        session, ctx.automation_id, {key: value}, kv_config.kv_secret, {key: existing}
    )
    await _bump_version(session, meta)

    return KVKeyResponse(key=key, value=popped)


@router.post("/{key}/rpop")
async def rpop(
    key: ValidatedKey,
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVKeyResponse:
    """Pop a value from the right (back) of a list.

    Returns null if key doesn't exist or list is empty.
    """
    kv_config = get_config().kv

    meta, existing = await _lock_existing_key(
        session, ctx.automation_id, key, kv_config.kv_lock_timeout_ms
    )

    if existing is None:
        return KVKeyResponse(key=key, value=None)

    value = _decrypt_value(kv_config.kv_secret, existing)
    require_list(value)

    if len(value) == 0:
        return KVKeyResponse(key=key, value=None)

    popped = value.pop()
    await _upsert_key_rows(
        session, ctx.automation_id, {key: value}, kv_config.kv_secret, {key: existing}
    )
    await _bump_version(session, meta)

    return KVKeyResponse(key=key, value=popped)


@router.get("/{key}/len")
async def list_length(
    key: ValidatedKey,
    ctx: KVAuthContext = Depends(get_kv_auth_context),
    session: AsyncSession = Depends(get_session),
) -> KVListLengthResponse:
    """Get the length of a list."""
    kv_config = get_config().kv

    result = await session.execute(
        select(AutomationKV).where(
            AutomationKV.automation_id == ctx.automation_id,
            AutomationKV.key == key,
        )
    )
    row = result.scalars().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="key_not_found",
        )

    value = _decrypt_value(kv_config.kv_secret, row)
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
    ctx: KVAuthContext = Depends(get_kv_auth_context),
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
    - 413: Payload too large (a single value exceeds the per-value size limit)
    """
    kv_config = get_config().kv

    # Only the keys the batch touches are loaded and locked; the metadata row is
    # locked first, then those key rows in sorted order.
    touched = {op.key for op in body.operations}
    meta, existing_rows = await _lock_write(
        session, ctx.automation_id, touched, kv_config.kv_lock_timeout_ms
    )

    # Check version if specified
    current_version = int(meta.version)
    if body.if_version is not None and current_version != body.if_version:
        _raise_version_conflict(body.if_version, current_version)

    state = {
        key: _decrypt_value(kv_config.kv_secret, row)
        for key, row in existing_rows.items()
    }

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

    # Validate the size of each resulting value (not their combined size).
    _check_batch_value_sizes(state, kv_config)

    # Persist only the keys the batch touched.
    await _upsert_key_rows(
        session, ctx.automation_id, state, kv_config.kv_secret, existing_rows
    )
    new_version = await _bump_version(session, meta)

    return KVBatchResponse(version=new_version, results=results)

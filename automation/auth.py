"""Authentication for the automations service API.

MVP approach: The caller passes an OpenHands API key in the Authorization header.
We validate it against the OpenHands API /api/keys/current endpoint to get
the user and organization identity.
"""

import logging
import uuid
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, Request, status

from automation.config import get_settings

logger = logging.getLogger('automation.auth')

# Default timeout for HTTP client
HTTP_CLIENT_TIMEOUT = 10.0


def create_http_client() -> httpx.AsyncClient:
    """Create a new httpx client for auth requests."""
    return httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT)


def get_http_client(request: Request) -> httpx.AsyncClient:
    """FastAPI dependency to get the shared httpx client from app.state.

    The client is created during app startup and stored in app.state.http_client.
    This enables proper dependency injection and makes testing easier.
    """
    client: httpx.AsyncClient | None = getattr(request.app.state, 'http_client', None)
    if client is None or client.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='HTTP client not initialized',
        )
    return client


@dataclass
class AuthenticatedUser:
    user_id: uuid.UUID
    org_id: uuid.UUID
    api_key: str  # The raw API key (needed for downstream API calls)


async def authenticate_request(
    request: Request,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> AuthenticatedUser:
    """Extract and validate the OpenHands API key from the Authorization header.

    Calls the OpenHands API /api/keys/current to verify the key and get
    user/org identity.
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Missing or invalid Authorization header. '
            'Expected: Bearer <api_key>',
        )

    api_key = auth_header.removeprefix('Bearer ').strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Empty API key',
        )

    settings = get_settings()
    try:
        resp = await client.get(
            f'{settings.openhands_api_base_url}/api/keys/current',
            headers={'Authorization': f'Bearer {api_key}'},
        )
    except httpx.RequestError as e:
        logger.error('Failed to reach OpenHands API for auth: %s', e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Failed to validate API key against OpenHands',
        )

    if resp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired API key',
        )
    if resp.status_code != 200:
        logger.error(
            'Unexpected status from OpenHands /api/keys/current: %s',
            resp.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Unexpected response from OpenHands API',
        )

    data = resp.json()
    user_id = data.get('user_id')
    org_id = data.get('org_id')
    if not user_id or not org_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Could not determine user/org identity from OpenHands API',
        )

    try:
        user_uuid = uuid.UUID(str(user_id))
        org_uuid = uuid.UUID(str(org_id))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Invalid user_id or org_id format from OpenHands API',
        )

    return AuthenticatedUser(user_id=user_uuid, org_id=org_uuid, api_key=api_key)

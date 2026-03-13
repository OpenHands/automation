"""Authentication for the automations service API.

MVP approach: The caller passes an OpenHands API key in the Authorization header.
We validate it against the OpenHands SaaS V1 API to get the user identity.
"""

import logging
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request, status

from automation.config import get_settings


logger = logging.getLogger(__name__)


@dataclass
class AuthenticatedUser:
    user_id: str
    api_key: str  # The raw API key (needed for downstream V1 calls)


async def authenticate_request(request: Request) -> AuthenticatedUser:
    """Extract and validate the OpenHands API key from the Authorization header.

    Calls the OpenHands V1 API /api/v1/user to verify the key and get user info.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. "
            "Expected: Bearer <api_key>",
        )

    api_key = auth_header.removeprefix("Bearer ").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty API key",
        )

    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.openhands_api_base_url}/api/v1/user",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.RequestError as e:
        logger.error("Failed to reach OpenHands API for auth: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to validate API key against OpenHands",
        )

    if resp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
        )
    if resp.status_code != 200:
        logger.error(
            "Unexpected status from OpenHands /api/v1/user: %s",
            resp.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected response from OpenHands API",
        )

    data = resp.json()
    user_id = data.get("id") or data.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not determine user identity from OpenHands API",
        )

    return AuthenticatedUser(user_id=str(user_id), api_key=api_key)

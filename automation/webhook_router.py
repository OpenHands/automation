"""FastAPI router for custom webhook management.

Provides CRUD operations for custom webhook integrations. Users can register
webhook sources (e.g., Stripe, custom services) and receive a signing secret
to configure in the external service.

Built-in integrations (GitHub) don't use this - they're configured via
environment variables.

TODO: Implement rate limiting for webhook endpoints. Consider:
- Per-org rate limits (e.g., 100 requests/minute across all sources)
- Per-org+source rate limits (e.g., 50 requests/minute per source)
- Use Redis or in-memory token bucket for distributed rate limiting
- Return 429 Too Many Requests with Retry-After header when exceeded
"""

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from automation.auth import AuthenticatedUser, authenticate_request
from automation.config import get_settings
from automation.db import get_session
from automation.models import CustomWebhook
from automation.schemas import (
    CustomWebhookCreate,
    CustomWebhookCreateResponse,
    CustomWebhookListResponse,
    CustomWebhookResponse,
    CustomWebhookSecretResponse,
    CustomWebhookUpdate,
)


router = APIRouter(prefix="/v1/webhooks", tags=["Webhooks"])


def _generate_webhook_secret() -> str:
    """Generate a cryptographically secure webhook secret."""
    # 32 bytes = 256 bits of entropy, URL-safe base64 encoded
    return f"whsec_{secrets.token_urlsafe(32)}"


def _build_webhook_url(org_id: uuid.UUID, source: str) -> str:
    """Build the webhook URL for a given org and source."""
    settings = get_settings()
    base_url = settings.resolved_base_url.rstrip("/")
    return f"{base_url}/v1/events/{org_id}/{source}"


def _webhook_to_response(
    webhook: CustomWebhook,
    include_secret: str | None = None,
) -> CustomWebhookResponse:
    """Convert a CustomWebhook model to a response schema.

    Args:
        webhook: The webhook model to convert.
        include_secret: If provided, return CustomWebhookCreateResponse with
            this secret value. If None, return base CustomWebhookResponse.
    """
    webhook_url = _build_webhook_url(webhook.org_id, webhook.source)

    base_fields = {
        "id": webhook.id,
        "org_id": webhook.org_id,
        "name": webhook.name,
        "source": webhook.source,
        "webhook_url": webhook_url,
        "event_key_expr": webhook.event_key_expr,
        "signature_header": webhook.signature_header,
        "enabled": webhook.enabled,
        "created_at": webhook.created_at,
        "updated_at": webhook.updated_at,
    }

    if include_secret is not None:
        return CustomWebhookCreateResponse(**base_fields, webhook_secret=include_secret)

    return CustomWebhookResponse(**base_fields)


def _webhook_to_create_response(
    webhook: CustomWebhook,
    generated_secret: str | None = None,
) -> CustomWebhookCreateResponse:
    """Convert a CustomWebhook model to a create response schema.

    Always returns CustomWebhookCreateResponse. The webhook_secret field
    is only populated if the system generated it (not user-provided).

    Args:
        webhook: The webhook model to convert.
        generated_secret: System-generated secret to include, or None if
            user provided their own (won't be echoed back).
    """
    webhook_url = _build_webhook_url(webhook.org_id, webhook.source)

    return CustomWebhookCreateResponse(
        id=webhook.id,
        org_id=webhook.org_id,
        name=webhook.name,
        source=webhook.source,
        webhook_url=webhook_url,
        webhook_secret=generated_secret,  # None if user-provided
        event_key_expr=webhook.event_key_expr,
        signature_header=webhook.signature_header,
        enabled=webhook.enabled,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    data: CustomWebhookCreate,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> CustomWebhookCreateResponse:
    """
    Register a new custom webhook source.

    Creates a webhook integration for receiving events from external services.

    **Secret handling:**
    - If `webhook_secret` is provided, it will be used (not echoed in response).
    - If not provided, one will be generated and included in the response.

    **Important:** Generated secrets are only shown once. Store securely.
    """
    # Determine secret: user-provided or system-generated
    generated_secret: str | None = None
    if data.webhook_secret:
        # User provided their own secret
        secret = data.webhook_secret
    else:
        # Generate one and remember to return it
        secret = _generate_webhook_secret()
        generated_secret = secret

    webhook = CustomWebhook(
        org_id=auth.org_id,
        name=data.name,
        source=data.source,
        webhook_secret=secret,
        event_key_expr=data.event_key_expr,
        signature_header=data.signature_header,
        enabled=True,
    )

    session.add(webhook)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Webhook source '{data.source}' already exists for this org",
        )

    await session.refresh(webhook)

    return _webhook_to_create_response(webhook, generated_secret=generated_secret)


@router.get("")
async def list_webhooks(
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CustomWebhookListResponse:
    """List all custom webhooks for the organization."""
    # Get total count
    count_query = select(func.count()).select_from(
        select(CustomWebhook.id).where(CustomWebhook.org_id == auth.org_id).subquery()
    )
    total = await session.scalar(count_query) or 0

    # Get webhooks
    query = (
        select(CustomWebhook)
        .where(CustomWebhook.org_id == auth.org_id)
        .order_by(CustomWebhook.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    webhooks = result.scalars().all()

    return CustomWebhookListResponse(
        webhooks=[_webhook_to_response(w) for w in webhooks],  # type: ignore[misc]
        total=total,
    )


@router.get("/{webhook_id}")
async def get_webhook(
    webhook_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> CustomWebhookResponse:
    """Get details of a specific webhook."""
    webhook = await session.get(CustomWebhook, webhook_id)

    if not webhook or webhook.org_id != auth.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )

    return _webhook_to_response(webhook)


@router.patch("/{webhook_id}")
async def update_webhook(
    webhook_id: uuid.UUID,
    data: CustomWebhookUpdate,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> CustomWebhookResponse:
    """
    Update a webhook's configuration.

    Updatable fields: `name`, `event_key_expr`, `signature_header`, `enabled`.
    The `source` cannot be changed after creation.
    """
    webhook = await session.get(CustomWebhook, webhook_id)

    if not webhook or webhook.org_id != auth.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )

    # Apply updates
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(webhook, field, value)

    await session.commit()
    await session.refresh(webhook)

    return _webhook_to_response(webhook)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    Delete a webhook.

    This will stop receiving events from this source. Any automations
    configured to trigger on this source will no longer fire.
    """
    webhook = await session.get(CustomWebhook, webhook_id)

    if not webhook or webhook.org_id != auth.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )

    await session.delete(webhook)
    await session.commit()


@router.post("/{webhook_id}/rotate-secret")
async def rotate_webhook_secret(
    webhook_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> CustomWebhookSecretResponse:
    """
    Rotate the webhook signing secret.

    Generates a new secret and invalidates the old one. You must update
    the secret in the external service after rotation.

    **Important:** The new secret is only shown once. Store it securely.
    """
    webhook = await session.get(CustomWebhook, webhook_id)

    if not webhook or webhook.org_id != auth.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )

    # Generate new secret
    new_secret = _generate_webhook_secret()
    webhook.webhook_secret = new_secret

    await session.commit()

    return CustomWebhookSecretResponse(webhook_secret=new_secret)

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
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.acknowledgements import DEFAULT_ACKNOWLEDGEMENT
from openhands.automation.auth import (
    AuthenticatedUser,
    require_permission,
)
from openhands.automation.config import get_settings
from openhands.automation.db import get_session
from openhands.automation.models import CustomWebhook
from openhands.automation.providers import DEFAULT_VERIFIER, TEAMS_VERIFIER
from openhands.automation.schemas import (
    CustomWebhookCreate,
    CustomWebhookCreateResponse,
    CustomWebhookListResponse,
    CustomWebhookResponse,
    CustomWebhookSecretResponse,
    CustomWebhookUpdate,
)
from openhands.automation.webhook_policy import (
    validate_source_webhook_compatibility,
    validate_webhook_protocol,
)


class WebhookRoute(APIRoute):
    """Validation failures must not echo input containing write-only tokens."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=[
                        {key: error[key] for key in ("loc", "msg", "type")}
                        for error in exc.errors()
                    ],
                ) from None

        return safe_handler


router = APIRouter(prefix="/v1/webhooks", tags=["Webhooks"], route_class=WebhookRoute)

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")


def _generate_webhook_secret() -> str:
    """Generate a cryptographically secure webhook secret."""
    # 32 bytes = 256 bits of entropy, URL-safe base64 encoded
    return f"whsec_{secrets.token_urlsafe(32)}"


def _build_webhook_url(org_id: uuid.UUID, source: str) -> str:
    """Build the webhook URL for a given org and source."""
    settings = get_settings()
    base_url = settings.resolved_base_url.rstrip("/")
    return f"{base_url}/v1/events/{org_id}/{source}"


def _webhook_to_response(webhook: CustomWebhook) -> CustomWebhookResponse:
    """Convert a CustomWebhook model to a response schema."""
    return CustomWebhookResponse(
        id=webhook.id,
        org_id=webhook.org_id,
        name=webhook.name,
        source=webhook.source,
        webhook_url=_build_webhook_url(webhook.org_id, webhook.source),
        event_key_expr=webhook.event_key_expr,
        signature_header=webhook.signature_header,
        # A cleared column verifies as the default.
        signature_scheme=webhook.signature_scheme or DEFAULT_VERIFIER,
        event_id_header=webhook.event_id_header,
        event_id_expr=webhook.event_id_expr,
        admission_expr=webhook.admission_expr,
        acknowledgement=webhook.acknowledgement or DEFAULT_ACKNOWLEDGEMENT,
        enabled=webhook.enabled,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
    )


def _webhook_to_create_response(
    webhook: CustomWebhook,
    generated_secret: str | None = None,
) -> CustomWebhookCreateResponse:
    """Convert a CustomWebhook model to a create response schema.

    Args:
        webhook: The webhook model to convert.
        generated_secret: System-generated secret to include, or None if
            user provided their own (won't be echoed back).
    """
    base = _webhook_to_response(webhook)
    return CustomWebhookCreateResponse(
        **base.model_dump(),
        webhook_secret=generated_secret,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    data: CustomWebhookCreate,
    auth: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CustomWebhookCreateResponse:
    """
    Register a new custom webhook source.

    Creates a webhook integration for receiving events from external services.

    **Secret handling:**
    - If `webhook_secret` is provided, it will be used (not echoed in response).
    - Teams requires its provider-issued token and does not generate one.
    - For other schemes, an omitted secret is generated and returned once.

    **Important:** Generated secrets are only shown once. Store securely.
    """
    await _validate_configuration(data.model_dump(), auth.org_id, data.source, session)

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
        signature_scheme=data.signature_scheme,
        event_id_header=data.event_id_header,
        event_id_expr=data.event_id_expr,
        admission_expr=data.admission_expr,
        acknowledgement=data.acknowledgement,
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
    auth: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
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
    auth: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
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
    auth: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CustomWebhookResponse:
    """
    Update a webhook's configuration.

    Updatable fields: `name`, `event_key_expr`, `signature_header`,
    `signature_scheme`, `event_id_header`, `event_id_expr`, `admission_expr`,
    `acknowledgement`, `enabled`, and write-only Teams `webhook_secret`.
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
    # Validate the merged view: clearing one field must not weaken a Teams
    # source, and a scheme transition must supply its own provider-issued key.
    merged = {
        field: update_data.get(field, getattr(webhook, field))
        for field in (
            "signature_scheme",
            "signature_header",
            "acknowledgement",
            "event_id_header",
            "event_id_expr",
            "admission_expr",
            "webhook_secret",
        )
    }
    if merged["signature_scheme"] == TEAMS_VERIFIER:
        if webhook.signature_scheme != TEAMS_VERIFIER and not update_data.get(
            "webhook_secret"
        ):
            raise HTTPException(
                422, "Switching to Teams requires a supplied Teams token"
            )
    elif "webhook_secret" in update_data:
        raise HTTPException(
            422,
            "Secret replacement is supported only for Teams tokens; use rotate-secret",
        )
    for field in ("name", "event_key_expr", "signature_header", "enabled"):
        if field in update_data and update_data[field] is None:
            raise HTTPException(422, f"{field} cannot be null")
    await _validate_configuration(
        merged,
        auth.org_id,
        webhook.source,
        session,
        check_continuation=(
            (merged["acknowledgement"] or DEFAULT_ACKNOWLEDGEMENT)
            != (webhook.acknowledgement or DEFAULT_ACKNOWLEDGEMENT)
            or update_data.get("enabled") is True
        ),
    )
    for field, value in update_data.items():
        setattr(webhook, field, value)

    await session.commit()
    await session.refresh(webhook)

    return _webhook_to_response(webhook)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
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
    auth: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session, scope="function"),
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

    if webhook.signature_scheme == TEAMS_VERIFIER:
        raise HTTPException(
            409, "Teams issues this token; replace it using the webhook update API"
        )

    # Generate new secret
    new_secret = _generate_webhook_secret()
    webhook.webhook_secret = new_secret

    await session.commit()

    return CustomWebhookSecretResponse(webhook_secret=new_secret)


async def _validate_configuration(
    values: dict[str, Any],
    org_id: uuid.UUID,
    source: str,
    session: AsyncSession,
    *,
    check_continuation: bool = True,
) -> None:
    try:
        validate_webhook_protocol(
            signature_scheme=values.get("signature_scheme"),
            signature_header=values["signature_header"],
            acknowledgement=values.get("acknowledgement"),
            event_id_header=values.get("event_id_header"),
            event_id_expr=values.get("event_id_expr"),
            admission_expr=values.get("admission_expr"),
            webhook_secret=values.get("webhook_secret"),
        )
        if check_continuation:
            await validate_source_webhook_compatibility(
                org_id, source, values.get("acknowledgement"), session
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

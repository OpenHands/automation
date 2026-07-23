"""Telemetry configuration endpoints for the automation service."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.db import get_session
from openhands.automation.middleware import TelemetryRequestContext
from openhands.automation.schemas import (
    TelemetryConsentRequest,
    TelemetryConsentResponse,
)
from openhands.automation.telemetry import (
    capture_automation_event,
    get_request_telemetry_context,
    set_stored_telemetry_consent,
)


router = APIRouter(prefix="/v1/telemetry", tags=["Telemetry"])

_require_manage_automations = require_permission("manage_automations")


@router.post("/consent")
async def set_telemetry_consent(
    body: TelemetryConsentRequest,
    request: Request,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> TelemetryConsentResponse:
    """Persist frontend telemetry consent for local backend capture decisions."""
    consent_granted = await set_stored_telemetry_consent(
        session,
        consent_granted=body.consent_granted,
        frontend_distinct_id=body.frontend_distinct_id,
    )

    if body.consent_granted:
        request_context = get_request_telemetry_context(request)
        await capture_automation_event(
            "automation_telemetry_consent_granted",
            request=request,
            user=user,
            properties={"consent_source": "agent_canvas"},
            request_context=TelemetryRequestContext(
                frontend_distinct_id=body.frontend_distinct_id,
                client_source=request_context.client_source,
                client_version=request_context.client_version,
            ),
            session=session,
        )

    await session.commit()
    return TelemetryConsentResponse(consent_granted=consent_granted)

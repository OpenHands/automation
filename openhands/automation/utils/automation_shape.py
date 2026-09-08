"""Validation helpers for executable automation definitions."""

from fastapi import HTTPException, status
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.models import Automation
from openhands.automation.schemas import (
    DraftValidationError,
    Trigger,
    validate_command_string,
)
from openhands.automation.utils.model_profiles import validate_model_profile_for_user
from openhands.automation.utils.tarball_validation import validate_tarball_path


_TRIGGER_ADAPTER = TypeAdapter(Trigger)


def _field_required(field: str) -> DraftValidationError:
    return DraftValidationError(
        field=field,
        code="field_required",
        message="Field required for an executable automation",
    )


def _shape_error(field: str, message: str) -> DraftValidationError:
    return DraftValidationError(field=field, code="invalid", message=message)


def _pydantic_errors(
    e: ValidationError, field_prefix: str
) -> list[DraftValidationError]:
    errors: list[DraftValidationError] = []
    for err in e.errors():
        loc = err.get("loc", ())
        path = field_prefix
        if loc:
            path += "." + ".".join(str(part) for part in loc if part != "tagged-union")
        errors.append(
            DraftValidationError(
                field=path,
                code=str(err.get("type", "invalid")),
                message=str(err.get("msg", "Invalid value")),
            )
        )
    return errors


async def validate_executable_automation_shape(
    automation: Automation,
    user: AuthenticatedUser,
    session: AsyncSession,
) -> list[DraftValidationError]:
    """Return validation errors that prevent an automation from executing.

    DRAFT rows may be partial, but any row that is enabled or dispatched must
    have the executable shape the dispatcher expects.
    """
    errors: list[DraftValidationError] = []

    if not automation.name or not automation.name.strip():
        errors.append(_field_required("name"))

    if automation.trigger is None:
        errors.append(_field_required("trigger"))
    else:
        try:
            _TRIGGER_ADAPTER.validate_python(automation.trigger)
        except ValidationError as e:
            errors.extend(_pydantic_errors(e, "trigger"))

    if not automation.tarball_path:
        errors.append(_field_required("tarball_path"))
    else:
        try:
            await validate_tarball_path(
                tarball_path=automation.tarball_path,
                user_id=user.user_id,
                org_id=user.org_id,
                session=session,
            )
        except HTTPException as e:
            errors.append(
                DraftValidationError(
                    field="tarball_path",
                    code=f"tarball_path_{e.status_code}",
                    message=str(e.detail),
                )
            )

    if not automation.entrypoint:
        errors.append(_field_required("entrypoint"))
    else:
        try:
            validate_command_string(
                automation.entrypoint, "entrypoint", allow_none=False
            )
        except ValueError as e:
            errors.append(_shape_error("entrypoint", str(e)))

    if automation.setup_script_path is not None:
        try:
            validate_command_string(automation.setup_script_path, "setup_script_path")
        except ValueError as e:
            errors.append(_shape_error("setup_script_path", str(e)))

    try:
        validate_model_profile_for_user(automation.model, user)
    except HTTPException as e:
        errors.append(
            DraftValidationError(
                field="model",
                code="model_profile_not_found",
                message=str(e.detail),
            )
        )

    return errors


async def assert_executable_automation_shape(
    automation: Automation,
    user: AuthenticatedUser,
    session: AsyncSession,
    *,
    message: str = "Automation is not executable",
) -> None:
    errors = await validate_executable_automation_shape(automation, user, session)
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": message,
                "errors": [error.model_dump() for error in errors],
            },
        )


def require_dispatch_fields(automation: Automation) -> tuple[str, str]:
    """Return dispatcher-critical fields after validation has made them non-null."""
    if automation.tarball_path is None or automation.entrypoint is None:
        raise ValueError("Automation is missing executable dispatch fields")
    return automation.tarball_path, automation.entrypoint

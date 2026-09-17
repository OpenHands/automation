"""Helpers for automation state compatibility."""

from enum import Enum
from typing import Any

from pydantic import TypeAdapter

from openhands.automation.models import AutomationState


_BOOL_ADAPTER: TypeAdapter[bool] = TypeAdapter(bool)


def _state_value(state: Any) -> Any:
    return state.value if isinstance(state, Enum) else state


def parse_automation_enabled(enabled: Any) -> bool | None:
    """Parse the deprecated enabled flag using Pydantic bool coercion."""
    return _BOOL_ADAPTER.validate_python(enabled) if enabled is not None else None


def model_automation_state(
    state: AutomationState | str | Enum | None, enabled: Any
) -> AutomationState:
    if state is not None:
        return AutomationState(_state_value(state))
    return (
        AutomationState.ACTIVE
        if parse_automation_enabled(enabled)
        else AutomationState.INACTIVE
    )


def automation_state_enabled(state: AutomationState | str | Enum | None) -> bool:
    return (
        AutomationState(_state_value(state)) == AutomationState.ACTIVE
        if state
        else True
    )

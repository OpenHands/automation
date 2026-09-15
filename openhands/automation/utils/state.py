"""Helpers for automation state compatibility."""

from enum import Enum
from typing import Any

from openhands.automation.models import AutomationState


def _state_value(state: Any) -> Any:
    return state.value if isinstance(state, Enum) else state


def model_automation_state(
    state: AutomationState | str | Enum | None, enabled: bool
) -> AutomationState:
    if state is not None:
        return AutomationState(_state_value(state))
    return AutomationState.ACTIVE if enabled else AutomationState.INACTIVE


def automation_state_enabled(state: AutomationState | str | Enum | None) -> bool:
    if state is None:
        return True
    return AutomationState(_state_value(state)) == AutomationState.ACTIVE

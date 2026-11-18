"""The stored plugin execution spec carried by Git Sync."""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue

from openhands.automation.plugin_specs import (
    AutomationPluginEntrypoint,
    AutomationPluginSource,
)
from openhands.automation.schemas import validate_command_string


PLUGIN_SPEC_RUNTIME = "plugin-spec://runtime"


class SyncedPluginSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin: AutomationPluginSource
    config: dict[str, JsonValue]
    required_plugins: list[AutomationPluginSource] | None = None
    agent_profile_name: str | None = None


def validate_synced_plugin_spec(value: Any) -> dict[str, Any]:
    # Manifest/config-schema validation remains in the execution preflight.
    return SyncedPluginSpec.model_validate(value).model_dump(exclude_unset=True)


def validate_synced_plugin_entrypoint(value: str) -> str:
    # Permit only the generated wrapper; validate its command and relative path.
    match = re.fullmatch(r'cd "\$AUTOMATION_PLUGIN_ROOT(?:/([^"]+))?" && (.+)', value)
    if match:
        AutomationPluginEntrypoint(command=match[2], working_dir=match[1] or ".")
        return value
    result = validate_command_string(value, "entrypoint", allow_none=False)
    assert result is not None
    return result

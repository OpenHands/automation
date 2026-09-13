"""Attach presets to the profile-selected agent already created by the service."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openhands.sdk.agent import AgentBase


def load_provisioned_agent() -> AgentBase | None:
    profile_id = os.environ.get("AUTOMATION_AGENT_PROFILE_ID")
    if not profile_id:
        return None
    from openhands.sdk.agent import AgentBase
    from openhands.sdk.client import AgentServerClient

    server = AgentServerClient(
        os.environ["AGENT_SERVER_URL"], os.environ["SESSION_API_KEY"]
    )
    try:
        info = server.get_conversation(os.environ["AUTOMATION_CONVERSATION_ID"])
    finally:
        server.close()
    launched = info.get("launched_agent_profile") or {}
    if launched.get("agent_profile_id") != profile_id:
        raise ValueError(
            "The provisioned conversation does not match its selected profile"
        )
    return AgentBase.model_validate(info["agent"])

"""Presets attach to the provisioned agent without resolving global defaults."""

from unittest.mock import Mock

import pytest

from openhands.automation.presets.agent_profile import load_provisioned_agent


def test_unprofiled_preset_keeps_existing_configuration(monkeypatch):
    monkeypatch.delenv("AUTOMATION_AGENT_PROFILE_ID", raising=False)
    assert load_provisioned_agent() is None


@pytest.mark.parametrize("matches", [True, False])
@pytest.mark.parametrize("kind", ["Agent", "ACPAgent"])
def test_profile_preset_requires_the_provisioned_identity(monkeypatch, matches, kind):
    from openhands.sdk import LLM, Agent

    agent = Agent(llm=LLM(model="test-model"))
    server = Mock()
    server.get_conversation.return_value = {
        "launched_agent_profile": {
            "agent_profile_id": "selected" if matches else "different"
        },
        "agent": (
            agent.model_dump(mode="json")
            if kind == "Agent"
            else {"kind": "ACPAgent", "acp_command": ["test-acp-server"]}
        ),
    }
    monkeypatch.setenv("AUTOMATION_AGENT_PROFILE_ID", "selected")
    monkeypatch.setenv("AUTOMATION_CONVERSATION_ID", "run")
    monkeypatch.setenv("AGENT_SERVER_URL", "http://server")
    monkeypatch.setenv("SESSION_API_KEY", "scoped-key")
    monkeypatch.setattr(
        "openhands.sdk.client.AgentServerClient", Mock(return_value=server)
    )
    if matches:
        actual = load_provisioned_agent()
        if kind == "Agent":
            assert isinstance(actual, Agent)
            assert actual.llm.model == "test-model"
        else:
            from openhands.sdk.agent import ACPAgent

            assert isinstance(actual, ACPAgent)
            assert actual.acp_command == ["test-acp-server"]
    else:
        with pytest.raises(ValueError, match="does not match"):
            load_provisioned_agent()
    server.get_conversation.assert_called_once_with("run")
    server.close.assert_called_once()

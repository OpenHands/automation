"""Compatibility backend for the original Docker-only profile setting."""

from openhands.automation.backends.conversation import ConversationAgentServerBackend


class DockerAgentServerBackend(ConversationAgentServerBackend):
    _runtime_kind = "docker"

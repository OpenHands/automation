"""One bundle-facing conversation contract for local and Docker workspaces."""

from __future__ import annotations

import httpx

from openhands.automation.backends.base import ExecutionContext
from openhands.automation.backends.local import LocalAgentServerBackend
from openhands.automation.utils.agent_server import (
    VerificationResult,
    verify_run_on_agent_server,
)
from openhands.sdk.client import AsyncAgentServerClient


class ConversationAgentServerBackend(LocalAgentServerBackend):
    agent_profile_id: str
    runtime_api_key: str = ""
    _runtime_kind: str | None = None

    async def _resolve_runtime(self, client: httpx.AsyncClient) -> str:
        if self._runtime_kind is None:
            info = await AsyncAgentServerClient(
                self.agent_server_url, self.api_key, http_client=client
            ).get_server_info()
            self._runtime_kind = info["conversation_runtime"]
            if self._runtime_kind not in ("local", "docker"):
                raise ValueError("Unsupported agent-server conversation runtime")
        return self._runtime_kind

    @property
    def api_prefix(self) -> str:
        return (
            AsyncAgentServerClient(self.agent_server_url, self.api_key)
            .runtime(str(self._run.id))
            .api_prefix
        )

    async def get_execution_context(
        self, client: httpx.AsyncClient
    ) -> ExecutionContext:
        runtime_kind = await self._resolve_runtime(client)
        server = AsyncAgentServerClient(
            self.agent_server_url, self.api_key, http_client=client
        )
        await server.create_conversation(
            conversation_id=str(self._run.id),
            agent_profile_id=self.agent_profile_id,
            working_dir=self.get_work_dir(str(self._run.id)),
            title=self._run.automation.name,
            max_iterations=160,
            tags={"automationrun": str(self._run.id)},
        )
        self.runtime_api_key = self.api_key if runtime_kind == "local" else ""
        if runtime_kind == "docker":
            try:
                self.runtime_api_key = await server.runtime(
                    str(self._run.id)
                ).get_session_key()
            except Exception:
                await self.release_context(
                    client, ExecutionContext(self.agent_server_url, self.api_key)
                )
                raise
        return ExecutionContext(
            agent_url=self.agent_server_url,
            session_key=self.api_key,
            api_prefix=self.api_prefix,
        )

    def build_env_vars(self) -> dict[str, str]:
        if not self.runtime_api_key:
            raise RuntimeError("Runtime credentials have not been provisioned")
        return {
            "AGENT_SERVER_URL": (
                self.sandbox_agent_server_url
                or (
                    "http://127.0.0.1:8000"
                    if self._runtime_kind == "docker"
                    else self.agent_server_url
                )
            ),
            "AUTOMATION_CONVERSATION_ID": str(self._run.id),
            "WORKSPACE_BASE": self.get_work_dir(str(self._run.id)),
            "SESSION_API_KEY": self.runtime_api_key,
        }

    def get_work_dir(self, run_id: str) -> str:
        if self._runtime_kind is None:
            raise RuntimeError(
                "Resolve the server runtime before selecting a workspace"
            )
        return (
            "/workspace"
            if self._runtime_kind == "docker"
            else super().get_work_dir(run_id)
        )

    async def release_context(
        self, client: httpx.AsyncClient, ctx: ExecutionContext
    ) -> None:
        if await self._resolve_runtime(client) == "local":
            return  # Persistent server and conversation history belong to the host.
        await (
            AsyncAgentServerClient(ctx.agent_url, self.api_key, http_client=client)
            .runtime(str(self._run.id))
            .release()
        )

    async def verify_run(self, run_id: str) -> VerificationResult:
        return await verify_run_on_agent_server(
            agent_url=self.agent_server_url,
            session_key=self.api_key,
            run_id=run_id,
            bash_command_id=self._run.bash_command_id,
            api_prefix=self.api_prefix,
        )

    async def cleanup_after_verification(self, run_id: str) -> None:  # noqa: ARG002
        async with httpx.AsyncClient() as client:
            await self.release_context(
                client, ExecutionContext(self.agent_server_url, self.api_key)
            )

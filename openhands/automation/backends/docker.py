"""Run local automation bundles inside agent-server Docker conversations."""

from __future__ import annotations

import httpx

from openhands.automation.backends.base import ExecutionContext
from openhands.automation.backends.local import LocalAgentServerBackend
from openhands.automation.utils.agent_server import (
    VerificationResult,
    verify_run_on_agent_server,
)


class DockerAgentServerBackend(LocalAgentServerBackend):
    agent_profile_id: str
    runtime_api_key: str = ""

    @property
    def api_prefix(self) -> str:
        return f"/api/conversations/{self._run.id}"

    async def get_execution_context(
        self, client: httpx.AsyncClient
    ) -> ExecutionContext:
        response = await client.post(
            f"{self.agent_server_url}/api/conversations",
            headers={"X-Session-API-Key": self.api_key},
            json={
                "conversation_id": str(self._run.id),
                "agent_profile_id": self.agent_profile_id,
                "workspace": {"kind": "LocalWorkspace", "working_dir": "/workspace"},
                "title": self._run.automation.name,
                "max_iterations": 160,
                "tags": {"automationrun": str(self._run.id)},
            },
            timeout=180,
        )
        response.raise_for_status()
        try:
            credentials = await client.post(
                f"{self.agent_server_url}{self.api_prefix}/runtime/credentials",
                headers={"X-Session-API-Key": self.api_key},
            )
            credentials.raise_for_status()
            self.runtime_api_key = credentials.json()["session_api_key"]
            if not self.runtime_api_key:
                raise ValueError("Runtime returned an empty session credential")
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
            "AGENT_SERVER_URL": "http://127.0.0.1:8000",
            "AUTOMATION_CONVERSATION_ID": str(self._run.id),
            "WORKSPACE_BASE": "/workspace",
            "SESSION_API_KEY": self.runtime_api_key,
        }

    def get_work_dir(self, run_id: str) -> str:  # noqa: ARG002
        return "/workspace"

    async def release_context(
        self, client: httpx.AsyncClient, ctx: ExecutionContext
    ) -> None:
        response = await client.delete(
            f"{ctx.agent_url}{self.api_prefix}/runtime",
            headers={"X-Session-API-Key": self.api_key},
            timeout=60,
        )
        if response.status_code != 404:
            response.raise_for_status()

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

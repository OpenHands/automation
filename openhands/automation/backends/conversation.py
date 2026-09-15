"""Execute an automation bundle inside an Agent Server conversation."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx

from openhands.automation.backends.base import ExecutionBackend, ExecutionContext
from openhands.automation.backends.local import LocalAgentServerBackend
from openhands.automation.models import AutomationRun
from openhands.automation.subjects import conversation_id_for
from openhands.automation.utils.agent_server import (
    VerificationResult,
    verify_run_on_agent_server,
)
from openhands.sdk import RemoteConversationControl
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.workspace import (
    AsyncRemoteWorkspace,
    LocalWorkspace,
    RemoteWorkspace,
)


class ConversationBackend(ExecutionBackend):
    """Add conversation lifecycle to an existing Agent Server backend."""

    def __init__(self, server: LocalAgentServerBackend, run: AutomationRun) -> None:
        if run.agent_profile_id is None:
            raise ValueError("Conversation execution requires an agent profile")
        self.server = server
        self.run = run
        self.runtime_api_key = ""
        self.runtime_kind: str | None = None

    @property
    def is_local_mode(self) -> bool:
        return True

    @property
    def conversation_id(self) -> UUID:
        automation = self.run.automation
        source = (automation.trigger or {}).get("source")
        if self.run.subject_key and source:
            return UUID(
                conversation_id_for(
                    automation.org_id,
                    automation.id,
                    source,
                    self.run.subject_key,
                )
            )
        return self.run.id

    async def _resolve_runtime(self) -> str:
        if self.runtime_kind is None:
            async with AsyncRemoteWorkspace(
                host=self.server.agent_server_url,
                api_key=self.server.api_key,
                working_dir="/",
            ) as workspace:
                info = await workspace.get_server_info()
            self.runtime_kind = info["conversation_runtime"]
            if self.runtime_kind not in ("local", "docker"):
                raise ValueError("Unsupported Agent Server conversation runtime")
        return self.runtime_kind

    def _create_conversation(self) -> None:
        workspace = RemoteWorkspace(
            host=self.server.agent_server_url,
            api_key=self.server.api_key,
            working_dir=self.get_work_dir(str(self.run.id)),
        )
        try:
            RemoteConversationControl.create(
                workspace=workspace,
                request=StartConversationRequest(
                    workspace=LocalWorkspace(working_dir=workspace.working_dir),
                    conversation_id=self.conversation_id,
                    agent_profile_id=self.run.agent_profile_id,
                    tags={"automationrun": str(self.run.id)},
                ),
            )
        finally:
            workspace.reset_client()

    async def get_execution_context(
        self, client: httpx.AsyncClient
    ) -> ExecutionContext:
        runtime_kind = await self._resolve_runtime()
        await asyncio.to_thread(self._create_conversation)
        self.runtime_api_key = self.server.api_key if runtime_kind == "local" else ""
        context = ExecutionContext(
            agent_url=self.server.agent_server_url,
            session_key=self.server.api_key,
            runtime_conversation_id=self.conversation_id,
        )
        if runtime_kind == "docker":
            try:
                async with AsyncRemoteWorkspace(
                    host=context.agent_url,
                    api_key=self.server.api_key,
                    working_dir="/",
                    runtime_conversation_id=self.conversation_id,
                ) as workspace:
                    self.runtime_api_key = await workspace.get_runtime_session_key()
            except Exception:
                await self.release_context(client, context)
                raise
        return context

    async def release_context(
        self,
        client: httpx.AsyncClient,  # noqa: ARG002
        ctx: ExecutionContext,
    ) -> None:
        if await self._resolve_runtime() == "local":
            return
        async with AsyncRemoteWorkspace(
            host=ctx.agent_url,
            api_key=self.server.api_key,
            working_dir="/",
            runtime_conversation_id=self.conversation_id,
        ) as workspace:
            await workspace.release_runtime()

    async def get_api_key(self) -> str:
        return await self.server.get_api_key()

    def build_env_vars(self) -> dict[str, str]:
        if not self.runtime_api_key:
            raise RuntimeError("Runtime credentials have not been provisioned")
        return {
            "AGENT_SERVER_URL": (
                self.server.sandbox_agent_server_url
                or (
                    "http://127.0.0.1:8000"
                    if self.runtime_kind == "docker"
                    else self.server.agent_server_url
                )
            ),
            "AUTOMATION_AGENT_PROFILE_ID": str(self.run.agent_profile_id),
            "AUTOMATION_CONVERSATION_ID": str(self.conversation_id),
            "SESSION_API_KEY": self.runtime_api_key,
            "WORKSPACE_BASE": self.get_work_dir(str(self.run.id)),
        }

    async def verify_run(self, run_id: str) -> VerificationResult:
        return await verify_run_on_agent_server(
            agent_url=self.server.agent_server_url,
            session_key=self.server.api_key,
            run_id=run_id,
            bash_command_id=self.run.bash_command_id,
            runtime_conversation_id=self.conversation_id,
        )

    async def cleanup_after_verification(self, run_id: str) -> None:  # noqa: ARG002
        async with httpx.AsyncClient() as client:
            await self.release_context(
                client,
                ExecutionContext(
                    self.server.agent_server_url,
                    self.server.api_key,
                    runtime_conversation_id=self.conversation_id,
                ),
            )

    def get_work_dir(self, run_id: str) -> str:
        if self.runtime_kind is None:
            raise RuntimeError("Resolve the runtime before selecting a workspace")
        if self.runtime_kind == "docker":
            return "/workspace"
        return self.server.get_work_dir(run_id)

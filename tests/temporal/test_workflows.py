"""Tests for Temporal workflows.

Uses WorkflowEnvironment with time-skipping to test workflows with mocked activities.

Note: These tests require the Temporal test server which is bundled with temporalio.
The time-skipping environment allows testing workflows with timers/delays quickly.
"""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from automation.temporal.types import (
    AutomationConfig,
    CleanupSandboxInput,
    CreateSandboxInput,
    DownloadTarballInput,
    ExecuteEntrypointInput,
    ExecutionResult,
    GetApiKeyInput,
    SandboxInfo,
    TriggerContext,
    UploadTarballInput,
    WorkflowInput,
    WorkflowResult,
)
from automation.temporal.workflows import AutomationWorkflow


# --- Mock Activities ---
# These mocked activities simulate successful execution paths


@activity.defn(name="get_api_key")
async def mock_get_api_key(input: GetApiKeyInput) -> str:
    """Mock activity that returns a test API key."""
    return f"sk-test-{input.user_id[:8]}"


@activity.defn(name="create_sandbox")
async def mock_create_sandbox(input: CreateSandboxInput) -> SandboxInfo:
    """Mock activity that creates a fake sandbox."""
    return SandboxInfo(
        sandbox_id=f"sandbox-{input.run_id}",
        agent_url="https://mock-agent.example.com",
        session_key="mock-session-key",
        api_key=input.api_key,
    )


@activity.defn(name="download_tarball")
async def mock_download_tarball(input: DownloadTarballInput) -> bytes:
    """Mock activity that returns fake tarball content."""
    return b"mock tarball content"


@activity.defn(name="upload_tarball")
async def mock_upload_tarball(input: UploadTarballInput) -> None:
    """Mock activity that simulates tarball upload."""
    pass


@activity.defn(name="execute_entrypoint")
async def mock_execute_entrypoint_success(
    input: ExecuteEntrypointInput,
) -> ExecutionResult:
    """Mock activity that simulates successful execution."""
    return ExecutionResult(
        success=True,
        exit_code=0,
        stdout="Execution completed successfully",
        stderr="",
    )


@activity.defn(name="cleanup_sandbox")
async def mock_cleanup_sandbox(input: CleanupSandboxInput) -> None:
    """Mock activity that simulates sandbox cleanup."""
    pass


# Mock activities for failure scenarios


def create_failing_execute_activity():
    """Create a mock execute activity that fails."""

    @activity.defn(name="execute_entrypoint")
    async def mock_execute_entrypoint_failure(
        input: ExecuteEntrypointInput,
    ) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="Error: Script failed",
            error="Script exited with code 1",
        )

    return mock_execute_entrypoint_failure


def create_tracking_cleanup_activity(cleanup_calls: list):
    """Create a cleanup activity that tracks calls."""

    @activity.defn(name="cleanup_sandbox")
    async def mock_cleanup_tracking(input: CleanupSandboxInput) -> None:
        cleanup_calls.append(input.sandbox_id)

    return mock_cleanup_tracking


# Standard set of mock activities for successful workflows
MOCK_ACTIVITIES_SUCCESS = [
    mock_get_api_key,
    mock_create_sandbox,
    mock_download_tarball,
    mock_upload_tarball,
    mock_execute_entrypoint_success,
    mock_cleanup_sandbox,
]


def make_automation_config(
    name: str = "Test Automation",
    tarball_path: str = "https://example.com/code.tar.gz",
    timeout_seconds: int = 300,
) -> AutomationConfig:
    """Helper to create test AutomationConfig."""
    return AutomationConfig(
        automation_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        org_id=str(uuid.uuid4()),
        name=name,
        tarball_path=tarball_path,
        entrypoint="python main.py",
        timeout_seconds=timeout_seconds,
    )


def make_workflow_input(
    config: AutomationConfig | None = None,
    trigger_type: str = "manual",
) -> WorkflowInput:
    """Helper to create test WorkflowInput."""
    if config is None:
        config = make_automation_config()
    return WorkflowInput(
        automation=config,
        trigger_context=TriggerContext(trigger_type=trigger_type),
        run_id=str(uuid.uuid4()),
    )


# Skip workflow tests if temporal test server is not available
# These tests require downloading the test server on first run
pytestmark = pytest.mark.skip(
    reason="Workflow tests require Temporal test server - run manually with: "
    "pytest tests/temporal/test_workflows.py --no-skip"
)


class TestAutomationWorkflow:
    """Tests for AutomationWorkflow.

    These tests use the Temporal time-skipping test server to run workflows
    with mocked activities. On first run, the test server binary will be
    downloaded automatically.
    """

    @pytest.fixture
    def workflow_input(self) -> WorkflowInput:
        return make_workflow_input()

    @pytest.mark.asyncio
    async def test_workflow_success(self, workflow_input: WorkflowInput):
        """Test successful workflow execution end-to-end."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AutomationWorkflow],
                activities=MOCK_ACTIVITIES_SUCCESS,
            ):
                result = await env.client.execute_workflow(
                    AutomationWorkflow.run,
                    workflow_input,
                    id=f"test-{workflow_input.run_id}",
                    task_queue="test-queue",
                )

                assert isinstance(result, WorkflowResult)
                assert result.success is True
                assert result.run_id == workflow_input.run_id
                assert result.exit_code == 0
                assert result.error is None

    @pytest.mark.asyncio
    async def test_workflow_with_internal_tarball(self):
        """Test workflow with internal tarball (oh-internal://)."""
        config = make_automation_config(
            tarball_path="oh-internal://uploads/user-123/code.tar.gz"
        )
        workflow_input = make_workflow_input(config=config)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AutomationWorkflow],
                activities=MOCK_ACTIVITIES_SUCCESS,
            ):
                result = await env.client.execute_workflow(
                    AutomationWorkflow.run,
                    workflow_input,
                    id=f"test-internal-{workflow_input.run_id}",
                    task_queue="test-queue",
                )

                assert result.success is True

    @pytest.mark.asyncio
    async def test_workflow_execution_failure(self, workflow_input: WorkflowInput):
        """Test workflow handles execution failure gracefully."""
        activities_with_failure = [
            mock_get_api_key,
            mock_create_sandbox,
            mock_download_tarball,
            mock_upload_tarball,
            create_failing_execute_activity(),
            mock_cleanup_sandbox,
        ]

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AutomationWorkflow],
                activities=activities_with_failure,
            ):
                result = await env.client.execute_workflow(
                    AutomationWorkflow.run,
                    workflow_input,
                    id=f"test-failure-{workflow_input.run_id}",
                    task_queue="test-queue",
                )

                assert result.success is False
                assert result.exit_code == 1
                assert result.error is not None

    @pytest.mark.asyncio
    async def test_cleanup_always_runs(self, workflow_input: WorkflowInput):
        """Test that cleanup activity runs even when execution fails."""
        cleanup_calls: list[str] = []

        activities_with_failure_and_tracking = [
            mock_get_api_key,
            mock_create_sandbox,
            mock_download_tarball,
            mock_upload_tarball,
            create_failing_execute_activity(),
            create_tracking_cleanup_activity(cleanup_calls),
        ]

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AutomationWorkflow],
                activities=activities_with_failure_and_tracking,
            ):
                result = await env.client.execute_workflow(
                    AutomationWorkflow.run,
                    workflow_input,
                    id=f"test-cleanup-{workflow_input.run_id}",
                    task_queue="test-queue",
                )

                # Execution failed but cleanup still ran
                assert result.success is False
                assert len(cleanup_calls) == 1

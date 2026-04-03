"""Temporal Workflow definitions for automation execution.

Workflows are the core orchestration units. They coordinate activities,
handle failures, and maintain durable state. Workflows must be deterministic
- they cannot make HTTP calls, access databases, or use random/time directly.
All side effects must go through activities.

The AutomationWorkflow is the main workflow that:
1. Gets a per-user API key
2. Creates a sandbox
3. Downloads/uploads the tarball
4. Executes the entrypoint
5. Cleans up the sandbox (even on failure)
"""

import json
import logging
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

# Import activity stubs - these are used for type hints and to call activities
with workflow.unsafe.imports_passed_through():
    from automation.config import get_settings
    from automation.temporal.types import (
        CleanupSandboxInput,
        CreateSandboxInput,
        DownloadTarballInput,
        ExecuteEntrypointInput,
        ExecutionResult,
        GetApiKeyInput,
        SandboxInfo,
        UploadTarballInput,
        WorkflowInput,
        WorkflowResult,
    )
    from automation.utils.tarball_validation import (
        is_http_url,
        parse_internal_upload_id,
    )


logger = logging.getLogger(__name__)


# Retry policies for different activity types
API_KEY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=5,
    non_retryable_error_types=["ValueError"],  # Invalid user/org is permanent
)

SANDBOX_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)

TARBALL_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=3,
    non_retryable_error_types=["ValueError"],  # Missing tarball is permanent
)

# No retries for execution - if it fails, it fails
EXECUTION_RETRY_POLICY = RetryPolicy(
    maximum_attempts=1,
)

CLEANUP_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@workflow.defn
class AutomationWorkflow:
    """Main workflow for executing an automation.

    This workflow orchestrates the full lifecycle of an automation run:
    1. Fetch per-user API key from OpenHands SaaS
    2. Create sandbox and wait until RUNNING
    3. Get tarball into sandbox (download internal or fetch external)
    4. Execute entrypoint command
    5. Clean up sandbox (always, even on failure)

    The workflow is durable - if the worker crashes at any point, Temporal
    will resume execution from the last completed activity.
    """

    @workflow.run
    async def run(self, input: WorkflowInput) -> WorkflowResult:
        """Execute the automation workflow."""
        workflow.logger.info(
            "Starting automation workflow",
            extra={
                "run_id": input.run_id,
                "automation_id": input.automation.automation_id,
                "name": input.automation.name,
            },
        )

        settings = get_settings()
        sandbox_info: SandboxInfo | None = None
        api_key: str | None = None

        try:
            # 1. Get per-user API key
            api_key = await workflow.execute_activity(
                "get_api_key",
                GetApiKeyInput(
                    user_id=input.automation.user_id,
                    org_id=input.automation.org_id,
                    run_id=input.run_id,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=API_KEY_RETRY_POLICY,
            )

            # 2. Create sandbox
            sandbox_info = await workflow.execute_activity(
                "create_sandbox",
                CreateSandboxInput(
                    api_url=settings.openhands_api_base_url,
                    api_key=api_key,
                    run_id=input.run_id,
                ),
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=SANDBOX_RETRY_POLICY,
            )

            # 3. Get tarball into sandbox
            tarball_path = input.automation.tarball_path

            if is_http_url(tarball_path):
                # External URL - download directly in sandbox
                await workflow.execute_activity(
                    "upload_tarball",
                    UploadTarballInput(
                        sandbox_info=sandbox_info,
                        tarball_url=tarball_path,
                        run_id=input.run_id,
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=TARBALL_RETRY_POLICY,
                )
            else:
                # Internal tarball - download from storage, upload to sandbox
                upload_id = parse_internal_upload_id(tarball_path)
                if upload_id is None:
                    raise ValueError(f"Invalid tarball_path: {tarball_path}")

                tarball_data = await workflow.execute_activity(
                    "download_tarball",
                    DownloadTarballInput(
                        upload_id=str(upload_id),
                        run_id=input.run_id,
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=TARBALL_RETRY_POLICY,
                )

                await workflow.execute_activity(
                    "upload_tarball",
                    UploadTarballInput(
                        sandbox_info=sandbox_info,
                        tarball_data=tarball_data,
                        run_id=input.run_id,
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=TARBALL_RETRY_POLICY,
                )

            # 4. Build env vars for execution
            env_vars = {
                "OPENHANDS_API_KEY": api_key,
                "OPENHANDS_CLOUD_API_URL": settings.openhands_api_base_url,
                "SANDBOX_ID": sandbox_info.sandbox_id,
                "SESSION_API_KEY": sandbox_info.session_key,
                "AUTOMATION_RUN_ID": input.run_id,
            }

            # Add callback URL if provided
            if input.callback_url:
                env_vars["AUTOMATION_CALLBACK_URL"] = input.callback_url

            # Add trigger context
            event_payload = {
                "trigger": input.automation.trigger,
                "automation_id": input.automation.automation_id,
                "automation_name": input.automation.name,
            }
            env_vars["AUTOMATION_EVENT_PAYLOAD"] = json.dumps(event_payload)

            # 5. Execute entrypoint
            execution_result: ExecutionResult = await workflow.execute_activity(
                "execute_entrypoint",
                ExecuteEntrypointInput(
                    sandbox_info=sandbox_info,
                    entrypoint=input.automation.entrypoint,
                    env_vars=env_vars,
                    timeout_seconds=input.automation.timeout_seconds,
                    run_id=input.run_id,
                ),
                start_to_close_timeout=timedelta(
                    seconds=input.automation.timeout_seconds + 120
                ),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=EXECUTION_RETRY_POLICY,
            )

            workflow.logger.info(
                "Automation completed",
                extra={
                    "run_id": input.run_id,
                    "success": execution_result.success,
                    "exit_code": execution_result.exit_code,
                },
            )

            return WorkflowResult(
                success=execution_result.success,
                run_id=input.run_id,
                sandbox_id=sandbox_info.sandbox_id,
                exit_code=execution_result.exit_code,
                error=execution_result.error,
                conversation_id=execution_result.conversation_id,
            )

        except ActivityError as e:
            workflow.logger.error(
                "Activity failed",
                extra={
                    "run_id": input.run_id,
                    "error": str(e),
                },
            )
            return WorkflowResult(
                success=False,
                run_id=input.run_id,
                sandbox_id=sandbox_info.sandbox_id if sandbox_info else None,
                error=str(e),
            )

        except Exception as e:
            workflow.logger.exception(
                "Workflow failed",
                extra={"run_id": input.run_id},
            )
            return WorkflowResult(
                success=False,
                run_id=input.run_id,
                sandbox_id=sandbox_info.sandbox_id if sandbox_info else None,
                error=str(e),
            )

        finally:
            # 6. Always clean up sandbox
            if sandbox_info and api_key:
                try:
                    await workflow.execute_activity(
                        "cleanup_sandbox",
                        CleanupSandboxInput(
                            api_url=settings.openhands_api_base_url,
                            api_key=api_key,
                            sandbox_id=sandbox_info.sandbox_id,
                            run_id=input.run_id,
                        ),
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=CLEANUP_RETRY_POLICY,
                    )
                except Exception as cleanup_error:
                    workflow.logger.warning(
                        "Failed to cleanup sandbox",
                        extra={
                            "run_id": input.run_id,
                            "sandbox_id": sandbox_info.sandbox_id,
                            "error": str(cleanup_error),
                        },
                    )


# List of all workflows for worker registration
ALL_WORKFLOWS = [
    AutomationWorkflow,
]

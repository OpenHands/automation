"""Temporal workflow execution for automations.

This package provides durable workflow execution using Temporal:

- activities: Activity definitions for sandbox operations (API key, sandbox
  creation, tarball handling, entrypoint execution, cleanup)
- workflows: AutomationWorkflow orchestrates the full automation lifecycle
- worker: Temporal worker setup for processing tasks
- client: Temporal client factory for connecting to the service
- schedules: Temporal Schedule management for cron automations
- types: Data classes for workflow inputs/outputs

The main application (automation.app) uses this package to:
1. Run a Temporal worker as a background task
2. Create Temporal Schedules when automations are created
3. Start workflows when automations are manually triggered

To run a standalone worker:
    python -m automation.temporal.worker
"""

from automation.temporal.activities import (
    ALL_ACTIVITIES,
    cleanup_sandbox,
    create_sandbox,
    download_tarball,
    execute_entrypoint,
    get_api_key,
    upload_tarball,
)
from automation.temporal.client import (
    close_temporal_client,
    create_temporal_client,
    get_temporal_client,
)
from automation.temporal.types import (
    AutomationConfig,
    ExecutionResult,
    SandboxInfo,
    TriggerContext,
    WorkflowInput,
    WorkflowResult,
)
from automation.temporal.schedules import (
    create_schedule,
    delete_schedule,
    pause_schedule,
    trigger_schedule,
    unpause_schedule,
    update_schedule,
)
from automation.temporal.worker import create_worker, run_worker
from automation.temporal.workflows import ALL_WORKFLOWS, AutomationWorkflow


__all__ = [
    # Activities
    "get_api_key",
    "create_sandbox",
    "download_tarball",
    "upload_tarball",
    "execute_entrypoint",
    "cleanup_sandbox",
    "ALL_ACTIVITIES",
    # Workflows
    "AutomationWorkflow",
    "ALL_WORKFLOWS",
    # Data classes
    "AutomationConfig",
    "TriggerContext",
    "WorkflowInput",
    "WorkflowResult",
    "SandboxInfo",
    "ExecutionResult",
    # Client
    "get_temporal_client",
    "create_temporal_client",
    "close_temporal_client",
    # Worker
    "create_worker",
    "run_worker",
    # Schedules
    "create_schedule",
    "update_schedule",
    "delete_schedule",
    "pause_schedule",
    "unpause_schedule",
    "trigger_schedule",
]

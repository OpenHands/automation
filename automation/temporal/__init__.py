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

Note: This __init__.py intentionally does NOT import activities, workflows,
or worker modules at package level. Those modules import httpx and other
libraries that conflict with Temporal's workflow sandbox import system.
Import them directly when needed:
    from automation.temporal.activities import ALL_ACTIVITIES
    from automation.temporal.workflows import ALL_WORKFLOWS
"""

# Only import modules that don't have heavy dependencies (no httpx, sqlalchemy, etc.)
# These are safe to import at package level
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

# DO NOT import these at package level - they contain httpx, sqlalchemy, or
# other imports that conflict with Temporal's workflow sandbox:
# - activities (imports httpx)
# - workflows (imports activities transitively via the sandbox)
# - worker (imports both)
# - schedules (imports automation.models which uses sqlalchemy)

__all__ = [
    # Data classes (safe - no heavy deps)
    "AutomationConfig",
    "TriggerContext",
    "WorkflowInput",
    "WorkflowResult",
    "SandboxInfo",
    "ExecutionResult",
    # Client (safe - only temporalio)
    "get_temporal_client",
    "create_temporal_client",
    "close_temporal_client",
]

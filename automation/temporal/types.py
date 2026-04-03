"""Data classes for Temporal workflow inputs and outputs.

These are serializable data classes used to pass data between workflows
and activities. They must be JSON-serializable for Temporal's data converter.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class AutomationConfig:
    """Configuration for an automation run, passed as workflow input."""

    automation_id: str
    user_id: str
    org_id: str
    name: str
    tarball_path: str
    entrypoint: str
    timeout_seconds: int
    trigger: dict = field(default_factory=dict)
    setup_script_path: str | None = None


@dataclass(frozen=True)
class TriggerContext:
    """Context about what triggered this automation run."""

    trigger_type: str  # "cron", "manual", "webhook"
    scheduled_time: str | None = None  # ISO format for cron triggers
    triggered_by: str | None = None  # user_id for manual triggers


@dataclass(frozen=True)
class WorkflowInput:
    """Input to the AutomationWorkflow."""

    automation: AutomationConfig
    trigger_context: TriggerContext
    run_id: str  # Database run ID for status tracking
    callback_url: str | None = None  # URL for SDK to POST completion status


@dataclass(frozen=True)
class SandboxInfo:
    """Information about a created sandbox."""

    sandbox_id: str
    agent_url: str
    session_key: str
    api_key: str  # The per-user API key used to create the sandbox


@dataclass(frozen=True)
class ExecutionResult:
    """Result of executing the entrypoint in the sandbox."""

    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class WorkflowResult:
    """Final result of the AutomationWorkflow."""

    success: bool
    run_id: str
    sandbox_id: str | None = None
    exit_code: int | None = None
    error: str | None = None
    conversation_id: str | None = None
    started_at: str | None = None  # ISO format
    completed_at: str | None = None  # ISO format


# Activity input/output types


@dataclass(frozen=True)
class GetApiKeyInput:
    """Input for get_api_key activity."""

    user_id: str
    org_id: str
    run_id: str


@dataclass(frozen=True)
class CreateSandboxInput:
    """Input for create_sandbox activity."""

    api_url: str
    api_key: str
    run_id: str


@dataclass(frozen=True)
class DownloadTarballInput:
    """Input for download_tarball activity (internal tarballs)."""

    upload_id: str
    run_id: str


@dataclass(frozen=True)
class UploadTarballInput:
    """Input for upload_tarball activity."""

    sandbox_info: SandboxInfo
    tarball_data: bytes | None = None  # For internal tarballs (uploaded to sandbox)
    tarball_url: str | None = None  # For external tarballs (downloaded in sandbox)
    run_id: str = ""


@dataclass(frozen=True)
class ExecuteEntrypointInput:
    """Input for execute_entrypoint activity."""

    sandbox_info: SandboxInfo
    entrypoint: str
    env_vars: dict = field(default_factory=dict)
    timeout_seconds: int = 600
    run_id: str = ""


@dataclass(frozen=True)
class CleanupSandboxInput:
    """Input for cleanup_sandbox activity."""

    api_url: str
    api_key: str
    sandbox_id: str
    run_id: str = ""

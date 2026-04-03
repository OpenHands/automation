"""Tests for Temporal dataclasses."""

import pytest

from automation.temporal.types import (
    AutomationConfig,
    CleanupSandboxInput,
    CreateSandboxInput,
    ExecutionResult,
    GetApiKeyInput,
    SandboxInfo,
    TriggerContext,
    WorkflowInput,
    WorkflowResult,
)


class TestAutomationConfig:
    """Tests for AutomationConfig dataclass."""

    def test_create_basic(self):
        """Test creating a basic AutomationConfig."""
        config = AutomationConfig(
            automation_id="test-id",
            user_id="user-123",
            org_id="org-456",
            name="Test Automation",
            tarball_path="oh-internal://uploads/abc123",
            entrypoint="python main.py",
            timeout_seconds=600,
        )

        assert config.automation_id == "test-id"
        assert config.user_id == "user-123"
        assert config.org_id == "org-456"
        assert config.name == "Test Automation"
        assert config.tarball_path == "oh-internal://uploads/abc123"
        assert config.entrypoint == "python main.py"
        assert config.timeout_seconds == 600
        assert config.trigger == {}
        assert config.setup_script_path is None

    def test_create_with_trigger(self):
        """Test creating AutomationConfig with trigger."""
        trigger = {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"}
        config = AutomationConfig(
            automation_id="test-id",
            user_id="user-123",
            org_id="org-456",
            name="Test",
            tarball_path="https://example.com/code.tar.gz",
            entrypoint="./run.sh",
            timeout_seconds=300,
            trigger=trigger,
            setup_script_path="setup.sh",
        )

        assert config.trigger == trigger
        assert config.setup_script_path == "setup.sh"

    def test_is_frozen(self):
        """Test that AutomationConfig is immutable."""
        config = AutomationConfig(
            automation_id="test-id",
            user_id="user-123",
            org_id="org-456",
            name="Test",
            tarball_path="https://example.com/code.tar.gz",
            entrypoint="./run.sh",
            timeout_seconds=300,
        )

        with pytest.raises(AttributeError):
            config.name = "New Name"  # type: ignore


class TestWorkflowInput:
    """Tests for WorkflowInput dataclass."""

    def test_create(self):
        """Test creating WorkflowInput."""
        config = AutomationConfig(
            automation_id="auto-1",
            user_id="user-1",
            org_id="org-1",
            name="Test",
            tarball_path="https://example.com/code.tar.gz",
            entrypoint="python main.py",
            timeout_seconds=600,
        )
        trigger_context = TriggerContext(
            trigger_type="manual",
            triggered_by="user-1",
        )

        input = WorkflowInput(
            automation=config,
            trigger_context=trigger_context,
            run_id="run-123",
            callback_url="https://example.com/callback",
        )

        assert input.automation == config
        assert input.trigger_context == trigger_context
        assert input.run_id == "run-123"
        assert input.callback_url == "https://example.com/callback"


class TestSandboxInfo:
    """Tests for SandboxInfo dataclass."""

    def test_create(self):
        """Test creating SandboxInfo."""
        info = SandboxInfo(
            sandbox_id="sb-123",
            agent_url="https://agent.example.com",
            session_key="session-key-abc",
            api_key="api-key-xyz",
        )

        assert info.sandbox_id == "sb-123"
        assert info.agent_url == "https://agent.example.com"
        assert info.session_key == "session-key-abc"
        assert info.api_key == "api-key-xyz"


class TestExecutionResult:
    """Tests for ExecutionResult dataclass."""

    def test_successful_result(self):
        """Test creating a successful execution result."""
        result = ExecutionResult(
            success=True,
            exit_code=0,
            stdout="Hello World",
            stderr="",
        )

        assert result.success is True
        assert result.exit_code == 0
        assert result.stdout == "Hello World"
        assert result.stderr == ""
        assert result.error is None

    def test_failed_result(self):
        """Test creating a failed execution result."""
        result = ExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="Error: file not found",
            error="exit_code=1",
        )

        assert result.success is False
        assert result.exit_code == 1
        assert result.error == "exit_code=1"


class TestWorkflowResult:
    """Tests for WorkflowResult dataclass."""

    def test_successful_result(self):
        """Test creating a successful workflow result."""
        result = WorkflowResult(
            success=True,
            run_id="run-123",
            sandbox_id="sb-456",
            exit_code=0,
            conversation_id="conv-789",
        )

        assert result.success is True
        assert result.run_id == "run-123"
        assert result.sandbox_id == "sb-456"
        assert result.exit_code == 0
        assert result.error is None

    def test_failed_result(self):
        """Test creating a failed workflow result."""
        result = WorkflowResult(
            success=False,
            run_id="run-123",
            sandbox_id="sb-456",
            error="Sandbox creation failed",
        )

        assert result.success is False
        assert result.error == "Sandbox creation failed"


class TestActivityInputs:
    """Tests for activity input dataclasses."""

    def test_get_api_key_input(self):
        """Test GetApiKeyInput."""
        input = GetApiKeyInput(
            user_id="user-123",
            org_id="org-456",
            run_id="run-789",
        )

        assert input.user_id == "user-123"
        assert input.org_id == "org-456"
        assert input.run_id == "run-789"

    def test_create_sandbox_input(self):
        """Test CreateSandboxInput."""
        input = CreateSandboxInput(
            api_url="https://api.example.com",
            api_key="test-key",
            run_id="run-123",
        )

        assert input.api_url == "https://api.example.com"
        assert input.api_key == "test-key"
        assert input.run_id == "run-123"

    def test_cleanup_sandbox_input(self):
        """Test CleanupSandboxInput."""
        input = CleanupSandboxInput(
            api_url="https://api.example.com",
            api_key="test-key",
            sandbox_id="sb-123",
            run_id="run-456",
        )

        assert input.api_url == "https://api.example.com"
        assert input.sandbox_id == "sb-123"

"""Executable automation plugin specification support."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openhands.automation.observability_associations import (
    validate_observability_associations as validate_observability_association_exprs,
)
from openhands.automation.schemas import (
    PublicAutomationState,
    TemplateProvenance,
    Trigger,
    normalize_automation_state_enabled,
    reject_public_draft_state,
    validate_command_string,
)
from openhands.automation.utils.timeout import validate_automation_timeout
from openhands.sdk.plugin import PluginSource
from openhands.sdk.plugin.fetch import fetch_plugin_with_resolution


MANIFEST_NAME = "automation-plugin.json"
AUTOMATION_DIR = ".automation"


class PluginSpecError(ValueError):
    """Raised when an executable automation plugin spec is invalid."""


class AutomationPluginSource(BaseModel):
    """Source of an executable automation plugin or secondary plugin."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1)
    ref: str | None = Field(default=None, min_length=1)
    repo_path: str | None = Field(default=None, min_length=1)

    @field_validator("repo_path")
    @classmethod
    def validate_repo_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("/") or "\\" in value:
            raise ValueError("repo_path must be a relative POSIX path")
        if ".." in PurePosixPath(value).parts:
            raise ValueError("repo_path cannot contain '..'")
        return value

    def to_sdk_source(self) -> PluginSource:
        return PluginSource(source=self.source, ref=self.ref, repo_path=self.repo_path)


class AutomationPluginEntrypoint(BaseModel):
    """Script command declared by an executable automation plugin."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., min_length=1, max_length=500)
    working_dir: str = Field(default=".", min_length=1, max_length=200)
    setup_script: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        result = validate_command_string(value, "entrypoint", allow_none=False)
        assert result is not None
        return result

    @field_validator("working_dir", "setup_script")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = validate_command_string(value, "plugin path", allow_none=False)
        assert result is not None
        if result.startswith("/") or ".." in PurePosixPath(result).parts:
            raise ValueError("path must stay inside the plugin root")
        return result


class AutomationPluginProfileRequirement(BaseModel):
    """Profile requirement declared by an executable automation plugin."""

    model_config = ConfigDict(extra="forbid")

    required: bool = True
    default_name: str | None = Field(default=None, min_length=1, max_length=64)
    allowed_tools: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Exact tool names the selected agent profile is allowed to enable "
            "for this plugin. The profile must enable all and only these tools."
        ),
    )
    allowed_mcp_server_refs: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Exact MCP server refs the selected agent profile is allowed to "
            "enable for this plugin. The profile must enable all and only "
            "these MCP server refs."
        ),
    )
    allowed_secret_refs: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Exact secret refs the selected agent profile is allowed to expose "
            "to this plugin. The profile must expose all and only these secret refs."
        ),
    )

    @field_validator(
        "allowed_tools", "allowed_mcp_server_refs", "allowed_secret_refs"
    )
    @classmethod
    def validate_allowlist(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        seen: set[str] = set()
        result: list[str] = []
        for tool in value:
            if not tool or tool in seen:
                continue
            seen.add(tool)
            result.append(tool)
        return result


class AutomationPluginRuntime(BaseModel):
    """Runtime requirements for an executable automation plugin."""

    model_config = ConfigDict(extra="forbid")

    agent_profile: AutomationPluginProfileRequirement | None = None
    llm_profile: AutomationPluginProfileRequirement | None = None
    plugins: list[AutomationPluginSource] = Field(default_factory=list)


class AutomationPluginManifest(BaseModel):
    """Manifest stored as automation-plugin.json in an executable plugin."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openhands.automation.plugin.v1"]
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    entrypoint: AutomationPluginEntrypoint
    runtime: AutomationPluginRuntime = Field(default_factory=AutomationPluginRuntime)
    config_schema: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_config_schema(self) -> AutomationPluginManifest:
        schema_type = self.config_schema.get("type") if self.config_schema else "object"
        if schema_type != "object":
            raise ValueError("config_schema.type must be 'object' when provided")
        return self


class CreatePluginSpecAutomationRequest(BaseModel):
    """Create an automation backed by an executable plugin manifest."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=500)
    plugin: AutomationPluginSource
    trigger: Trigger
    observability_associations: dict[str, str] | None = None
    timeout: int | None = None
    keep_alive: bool | None = None
    template: TemplateProvenance | None = None
    enabled: bool = True
    state: PublicAutomationState | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_state_enabled(cls, data: Any) -> Any:
        return normalize_automation_state_enabled(data)

    @field_validator("state", mode="before")
    @classmethod
    def validate_public_state(cls, value: Any) -> Any:
        return reject_public_draft_state(value)

    @field_validator("observability_associations")
    @classmethod
    def validate_observability_associations(
        cls, value: dict[str, str] | None
    ) -> dict[str, str] | None:
        return validate_observability_association_exprs(value)

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, value: int | None) -> int | None:
        return validate_automation_timeout(value)


def _contained_path(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise PluginSpecError(f"Plugin path escapes plugin root: {relative}") from exc
    return path


def fetch_manifest(
    plugin: AutomationPluginSource,
) -> tuple[Path, str | None, AutomationPluginManifest]:
    """Fetch a plugin source and parse its automation-plugin.json manifest."""
    try:
        plugin_path, resolved_ref = fetch_plugin_with_resolution(
            source=plugin.source,
            ref=plugin.ref,
            repo_path=plugin.repo_path,
        )
    except Exception as exc:
        raise PluginSpecError(f"Failed to fetch automation plugin: {exc}") from exc

    manifest_path = _contained_path(plugin_path, MANIFEST_NAME)
    if not manifest_path.is_file():
        raise PluginSpecError(f"Automation plugin manifest not found: {MANIFEST_NAME}")
    try:
        manifest = AutomationPluginManifest.model_validate_json(
            manifest_path.read_text()
        )
    except Exception as exc:
        raise PluginSpecError(f"Invalid automation plugin manifest: {exc}") from exc

    _validate_manifest_paths(plugin_path, manifest)
    return plugin_path, resolved_ref, manifest


def _validate_manifest_paths(
    plugin_path: Path, manifest: AutomationPluginManifest
) -> None:
    working_dir = _contained_path(plugin_path, manifest.entrypoint.working_dir)
    if not working_dir.is_dir():
        raise PluginSpecError(
            f"Entrypoint working_dir does not exist: {manifest.entrypoint.working_dir}"
        )
    first_word = manifest.entrypoint.command.split()[0]
    if "/" in first_word:
        command_path = _contained_path(working_dir, first_word)
        if not command_path.exists():
            raise PluginSpecError(f"Entrypoint target does not exist: {first_word}")
    if manifest.entrypoint.setup_script:
        setup_path = _contained_path(plugin_path, manifest.entrypoint.setup_script)
        if not setup_path.is_file():
            raise PluginSpecError(
                f"Setup script does not exist: {manifest.entrypoint.setup_script}"
            )


def validate_required_plugins(
    plugins: list[AutomationPluginSource],
) -> list[dict[str, Any]]:
    """Fetch secondary plugins and return source specs with resolved refs."""
    resolved: list[dict[str, Any]] = []
    for plugin in plugins:
        try:
            _, resolved_ref = fetch_plugin_with_resolution(
                source=plugin.source,
                ref=plugin.ref,
                repo_path=plugin.repo_path,
            )
        except Exception as exc:
            raise PluginSpecError(
                f"Failed to fetch required plugin {plugin.source!r}: {exc}"
            ) from exc
        item = plugin.model_dump(exclude_none=True)
        if resolved_ref:
            item["resolved_ref"] = resolved_ref
        resolved.append(item)
    return resolved


def build_execution_payload(
    *,
    plugin_spec: dict[str, Any],
    agent_profile_id: str | None,
    agent_profile_name: str | None,
    llm_profile_name: str | None,
    work_dir: str,
) -> tuple[bytes, dict[str, str], dict[str, Any]]:
    """Fetch, validate, and package a plugin-backed automation run."""
    source = AutomationPluginSource.model_validate(plugin_spec["plugin"])
    config = plugin_spec.get("config") or {}
    plugin_path, resolved_ref, manifest = fetch_manifest(source)
    required_plugins = validate_required_plugins(manifest.runtime.plugins)
    if agent_profile_name is None and manifest.runtime.agent_profile is not None:
        agent_profile_name = manifest.runtime.agent_profile.default_name

    resolved_runtime = {
        "kind": "plugin_spec",
        "plugin": source.model_dump(exclude_none=True),
        "plugin_resolved_ref": resolved_ref,
        "entrypoint": entrypoint_for_manifest(manifest),
        "manifest": manifest.model_dump(mode="json"),
        "required_plugins": required_plugins,
        "agent_profile_id": agent_profile_id,
        "agent_profile_name": agent_profile_name,
        "llm_profile_name": llm_profile_name,
    }

    tarball = _build_plugin_tarball(
        plugin_path=plugin_path,
        manifest=manifest,
        config=config,
        resolved_runtime=resolved_runtime,
        required_plugins=required_plugins,
    )
    work_dir = work_dir.rstrip("/") or "/workspace/project"
    env = {
        "AUTOMATION_PLUGIN_ROOT": work_dir,
        "AUTOMATION_CONFIG_PATH": f"{work_dir}/.automation/config.json",
        "AUTOMATION_EXECUTION_SPEC_PATH": f"{work_dir}/.automation/execution.json",
        "AUTOMATION_REQUIRED_PLUGINS_PATH": f"{work_dir}/.automation/plugins.json",
    }
    if agent_profile_id:
        env["AUTOMATION_AGENT_PROFILE_ID"] = agent_profile_id
    if agent_profile_name:
        env["AUTOMATION_AGENT_PROFILE_NAME"] = agent_profile_name
    if llm_profile_name:
        env["AUTOMATION_LLM_PROFILE_NAME"] = llm_profile_name
    return tarball, env, resolved_runtime


def _build_plugin_tarball(
    *,
    plugin_path: Path,
    manifest: AutomationPluginManifest,
    config: dict[str, Any],
    resolved_runtime: dict[str, Any],
    required_plugins: list[dict[str, Any]],
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(plugin_path.rglob("*")):
            if path.is_dir() or path.is_symlink():
                continue
            rel = path.relative_to(plugin_path).as_posix()
            if any(part == ".git" for part in path.relative_to(plugin_path).parts):
                continue
            if rel == "setup.sh" and manifest.entrypoint.setup_script != "setup.sh":
                continue
            tar.add(path, arcname=rel, recursive=False)
        _add_json(tar, f"{AUTOMATION_DIR}/config.json", config)
        _add_json(tar, f"{AUTOMATION_DIR}/execution.json", resolved_runtime)
        _add_json(tar, f"{AUTOMATION_DIR}/plugins.json", required_plugins)
        if (
            manifest.entrypoint.setup_script
            and manifest.entrypoint.setup_script != "setup.sh"
        ):
            wrapper = f"#!/usr/bin/env bash\nbash {manifest.entrypoint.setup_script}\n"
            _add_bytes(tar, "setup.sh", wrapper.encode(), mode=0o755)
    buf.seek(0)
    return buf.read()


def _add_json(tar: tarfile.TarFile, name: str, data: Any) -> None:
    _add_bytes(tar, name, (json.dumps(data, indent=2) + "\n").encode())


def _add_bytes(
    tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def entrypoint_for_manifest(manifest: AutomationPluginManifest) -> str:
    if manifest.entrypoint.working_dir in ("", "."):
        return manifest.entrypoint.command
    return f"cd {manifest.entrypoint.working_dir} && {manifest.entrypoint.command}"

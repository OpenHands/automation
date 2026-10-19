"""Executable automation plugin specification support."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

from jsonschema import (
    Draft202012Validator,
    ValidationError as JSONSchemaValidationError,
    validators as jsonschema_validators,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

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
from openhands.sdk.extensions.fetch import SourceType, parse_extension_source
from openhands.sdk.plugin import PluginSource, PluginSourceError, preflight_plugin
from openhands.sdk.plugin.fetch import DEFAULT_CACHE_DIR, fetch_plugin_with_resolution


MANIFEST_NAME = "automation-plugin.json"
PLUGIN_MANIFEST_NAME = ".plugin/plugin.json"
PLUGIN_AUTOMATION_KEY = "automation"
AUTOMATION_DIR = ".automation"
PRESETS_DIR = Path(__file__).parent / "presets"
PLUGIN_SPEC_PRESET_DIR = PRESETS_DIR / "plugin_spec"
_PLUGIN_SPEC_PRESET_CACHE: dict[str, str] | None = None
_CONFIG_TEMPLATE_RE = re.compile(
    r"{{\s*(?P<json>json\s+)?config\.(?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}"
)
_TRIGGER_ADAPTER: TypeAdapter[Trigger] = TypeAdapter(Trigger)


class PluginSpecError(ValueError):
    """Raised when an executable automation plugin spec is invalid."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AutomationPluginSource(BaseModel):
    """Source of an executable automation plugin or secondary plugin."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1)
    ref: str | None = Field(default=None, min_length=1)
    repo_path: str | None = Field(default=None, min_length=1)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        source = value.strip()
        try:
            source_type, parsed_source = parse_extension_source(source)
        except Exception:
            source_type = None
            parsed_source = source
        parsed = urlparse(parsed_source)
        shorthand_path = source.removeprefix("github:")
        if (
            parsed.scheme in {"http", "https"}
            and (parsed.username is not None or parsed.password is not None)
        ) or (
            source_type == SourceType.GITHUB
            and ("@" in shorthand_path or ":" in shorthand_path)
        ):
            raise ValueError(
                "plugin source URLs must not include credentials; use a "
                "credential-free source"
            )
        return source

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
    """Runner command declared by an executable automation plugin."""

    model_config = ConfigDict(extra="forbid")

    runner: Literal["script", "conversation"] = "script"
    command: str = Field(..., min_length=1, max_length=500)
    arguments: str | None = Field(default=None, max_length=5000)
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

    @model_validator(mode="after")
    def validate_runner_fields(self) -> AutomationPluginEntrypoint:
        if self.runner == "script":
            if self.arguments is not None:
                raise ValueError(
                    "entrypoint.arguments is only valid for conversation runners"
                )
            return self
        if self.setup_script is not None:
            raise ValueError(
                "entrypoint.setup_script is not supported for conversation runners; "
                "the automation service provides the setup script for the stock SDK "
                "conversation runner. Use runner='script' if the plugin needs a "
                "custom setup script."
            )
        if self.working_dir != ".":
            raise ValueError("conversation runners must use working_dir='.'")
        if any(ch in self.command for ch in ("/", ":", "\\")):
            raise ValueError(
                "conversation runner command must be a plugin command name"
            )
        return self


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

    @field_validator("allowed_tools", "allowed_mcp_server_refs", "allowed_secret_refs")
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


class AutomationPluginDedupe(BaseModel):
    """Install dedupe hints declared by an automation plugin."""

    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(default_factory=list)


class AutomationPluginDefinition(BaseModel):
    """Automation metadata embedded under .plugin/plugin.json's automation key."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openhands.automation.plugin.v1"]
    entrypoint: AutomationPluginEntrypoint
    runtime: AutomationPluginRuntime = Field(default_factory=AutomationPluginRuntime)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    suggested_trigger: Trigger | None = None
    suggested_trigger_template: dict[str, Any] | None = None
    dedupe: AutomationPluginDedupe | None = None

    @model_validator(mode="after")
    def validate_config_schema(self) -> AutomationPluginDefinition:
        schema_type = self.config_schema.get("type") if self.config_schema else "object"
        if schema_type != "object":
            raise ValueError("config_schema.type must be 'object' when provided")
        if (
            self.suggested_trigger is not None
            and self.suggested_trigger_template is not None
        ):
            raise ValueError(
                "Only one of suggested_trigger or suggested_trigger_template may be set"
            )
        if self.entrypoint.runner == "conversation" and (
            self.runtime.agent_profile is None
            or not self.runtime.agent_profile.required
        ):
            raise ValueError(
                "conversation runners must declare a required runtime.agent_profile"
            )
        return self


class AutomationPluginManifest(AutomationPluginDefinition):
    """Complete automation plugin metadata resolved from a plugin manifest."""

    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class CreatePluginSpecAutomationRequest(BaseModel):
    """Create an automation backed by an executable plugin manifest."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=500)
    plugin: AutomationPluginSource
    config: dict[str, Any] = Field(default_factory=dict)
    trigger: Trigger | None = None
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


def validate_plugin_source_allowed(
    plugin: AutomationPluginSource,
    *,
    allow_local_sources: bool,
) -> None:
    """Reject plugin sources that are unsafe for automation-service preflight."""
    if allow_local_sources:
        return

    raw_source = plugin.source.strip()
    if raw_source.lower().startswith("file://"):
        raise PluginSpecError("file:// plugin sources are not allowed in cloud mode")

    try:
        source_type, url = parse_extension_source(raw_source)
    except Exception as exc:
        raise PluginSpecError(f"Invalid plugin source: {exc}") from exc

    if source_type == SourceType.LOCAL:
        raise PluginSpecError("Local plugin sources are not allowed in cloud mode")
    if source_type == SourceType.GITHUB:
        return

    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
        raise PluginSpecError(
            "Cloud plugin sources must use github:owner/repo or "
            "https://github.com/owner/repo.git"
        )


def _contained_path(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise PluginSpecError(f"Plugin path escapes plugin root: {relative}") from exc
    return path


def _cache_dir_for_plugin(plugin: AutomationPluginSource) -> Path | None:
    if not plugin.ref:
        return None
    cache_key = hashlib.sha256(f"{plugin.source}\0{plugin.ref}".encode()).hexdigest()[
        :16
    ]
    return DEFAULT_CACHE_DIR / "automation-plugin-refs" / cache_key


def fetch_manifest(
    plugin: AutomationPluginSource,
) -> tuple[Path, str | None, AutomationPluginManifest]:
    """Fetch a plugin source and parse its automation metadata."""
    try:
        plugin_path, resolved_ref = fetch_plugin_with_resolution(
            source=plugin.source,
            cache_dir=_cache_dir_for_plugin(plugin),
            ref=plugin.ref,
            repo_path=plugin.repo_path,
        )
    except Exception as exc:
        raise PluginSpecError(f"Failed to fetch automation plugin: {exc}") from exc

    try:
        manifest = _read_manifest(plugin_path)
    except Exception as exc:
        if isinstance(exc, PluginSpecError):
            raise
        raise PluginSpecError(f"Invalid automation plugin manifest: {exc}") from exc

    _validate_manifest_paths(plugin_path, manifest)
    return plugin_path, resolved_ref, manifest


def preflight_manifest(
    plugin: AutomationPluginSource,
    *,
    allow_local_sources: bool = True,
) -> tuple[str | None, AutomationPluginManifest]:
    """Strictly resolve a plugin source and parse its automation metadata."""
    validate_plugin_source_allowed(plugin, allow_local_sources=allow_local_sources)
    try:
        with preflight_plugin(
            source=plugin.source,
            ref=plugin.ref,
            repo_path=plugin.repo_path,
            allow_local=allow_local_sources,
        ) as checked:
            manifest = _read_manifest_from_plugin_metadata(checked.manifest)
            _validate_manifest_paths(checked.path, manifest)
            return checked.resolved_ref, manifest
    except PluginSourceError as exc:
        raise PluginSpecError(
            f"Failed to preflight automation plugin: {exc}",
            code=exc.code,
            status_code=exc.status_code,
        ) from exc
    except Exception as exc:
        if isinstance(exc, PluginSpecError):
            raise
        raise PluginSpecError(f"Failed to preflight automation plugin: {exc}") from exc


def _read_manifest_from_plugin_metadata(
    plugin_metadata: dict[str, Any],
) -> AutomationPluginManifest:
    automation_payload = plugin_metadata.get(PLUGIN_AUTOMATION_KEY)
    if not isinstance(automation_payload, dict):
        raise PluginSpecError(
            "Invalid automation plugin manifest: "
            f"{PLUGIN_MANIFEST_NAME}.{PLUGIN_AUTOMATION_KEY} must be an object"
        )
    data = {
        **automation_payload,
        "name": plugin_metadata.get("name"),
        "description": plugin_metadata.get("description"),
    }
    try:
        return AutomationPluginManifest.model_validate(data)
    except Exception as exc:
        raise PluginSpecError(
            "Invalid automation plugin manifest: "
            f"{PLUGIN_MANIFEST_NAME}.{PLUGIN_AUTOMATION_KEY}: {exc}"
        ) from exc


def _read_manifest(plugin_path: Path) -> AutomationPluginManifest:
    plugin_manifest_path = _contained_path(plugin_path, PLUGIN_MANIFEST_NAME)
    if plugin_manifest_path.is_file():
        try:
            plugin_metadata = json.loads(plugin_manifest_path.read_text())
        except Exception as exc:
            raise PluginSpecError(
                f"Invalid plugin manifest: {PLUGIN_MANIFEST_NAME}: {exc}"
            ) from exc
        if plugin_metadata.get(PLUGIN_AUTOMATION_KEY) is not None:
            return _read_manifest_from_plugin_metadata(plugin_metadata)

    legacy_manifest_path = _contained_path(plugin_path, MANIFEST_NAME)
    if not legacy_manifest_path.is_file():
        raise PluginSpecError(
            "Automation plugin manifest not found: "
            f"{PLUGIN_MANIFEST_NAME} with `{PLUGIN_AUTOMATION_KEY}` or {MANIFEST_NAME}"
        )
    try:
        return AutomationPluginManifest.model_validate_json(
            legacy_manifest_path.read_text()
        )
    except Exception as exc:
        raise PluginSpecError(f"Invalid automation plugin manifest: {exc}") from exc


def _validate_manifest_paths(
    plugin_path: Path, manifest: AutomationPluginManifest
) -> None:
    if manifest.entrypoint.runner == "conversation":
        command_path = _contained_path(
            plugin_path, f"commands/{manifest.entrypoint.command}.md"
        )
        if not command_path.is_file():
            raise PluginSpecError(
                "Conversation runner command does not exist: "
                f"commands/{manifest.entrypoint.command}.md"
            )
        return

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


def _extend_with_default(validator_class: type[Draft202012Validator]) -> Any:
    validate_properties = validator_class.VALIDATORS["properties"]

    def set_defaults(
        validator: Any, properties: dict[str, Any], instance: Any, schema: Any
    ) -> Any:
        if isinstance(instance, dict):
            for property_name, subschema in properties.items():
                if "default" in subschema:
                    instance.setdefault(property_name, subschema["default"])
        yield from validate_properties(validator, properties, instance, schema)

    return jsonschema_validators.extend(validator_class, {"properties": set_defaults})


_DEFAULTING_VALIDATOR = _extend_with_default(Draft202012Validator)


def validate_plugin_config(
    manifest: AutomationPluginManifest,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply JSON Schema defaults and validate install-time plugin config."""
    plugin_config = dict(config or {})
    schema = manifest.config_schema or {"type": "object"}
    try:
        _DEFAULTING_VALIDATOR(schema).validate(plugin_config)
    except JSONSchemaValidationError as exc:
        raise PluginSpecError(
            f"Plugin automation config is invalid: {exc.message}"
        ) from exc
    return plugin_config


def _config_value(config: dict[str, Any], path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise PluginSpecError(
                f"Trigger template references missing config value: config.{path}"
            )
        current = current[part]
    return current


def _render_config_templates(value: Any, config: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {
            key: _render_config_templates(item, config) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_render_config_templates(item, config) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        resolved = _config_value(config, match.group("path"))
        if match.group("json"):
            return json.dumps(resolved)
        return str(resolved)

    return _CONFIG_TEMPLATE_RE.sub(replace, value)


def resolve_suggested_trigger(
    manifest: AutomationPluginManifest,
    config: dict[str, Any],
) -> Trigger | None:
    """Resolve static or config-templated suggested trigger metadata."""
    if manifest.suggested_trigger is not None:
        return manifest.suggested_trigger
    if manifest.suggested_trigger_template is None:
        return None
    rendered = _render_config_templates(manifest.suggested_trigger_template, config)
    try:
        return _TRIGGER_ADAPTER.validate_python(rendered)
    except Exception as exc:
        raise PluginSpecError(f"Rendered suggested trigger is invalid: {exc}") from exc


def validate_required_plugins(
    plugins: list[AutomationPluginSource],
    *,
    allow_local_sources: bool = True,
) -> list[dict[str, Any]]:
    """Preflight secondary plugins and return source specs with resolved refs."""
    resolved: list[dict[str, Any]] = []
    for plugin in plugins:
        validate_plugin_source_allowed(plugin, allow_local_sources=allow_local_sources)
        try:
            with preflight_plugin(
                source=plugin.source,
                ref=plugin.ref,
                repo_path=plugin.repo_path,
                allow_local=allow_local_sources,
            ) as checked:
                resolved_ref = checked.resolved_ref
        except Exception as exc:
            raise PluginSpecError(
                f"Failed to preflight required plugin {plugin.source!r}: {exc}"
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
    allow_local_sources: bool = True,
) -> tuple[bytes, dict[str, str], dict[str, Any], list[dict[str, Any]]]:
    """Preflight, validate, and package a plugin-backed automation run."""
    source = AutomationPluginSource.model_validate(plugin_spec["plugin"])
    requested_config = plugin_spec.get("config") or {}
    resolved_ref, manifest = preflight_manifest(
        source, allow_local_sources=allow_local_sources
    )
    config = validate_plugin_config(manifest, requested_config)
    required_plugins = validate_required_plugins(
        manifest.runtime.plugins,
        allow_local_sources=allow_local_sources,
    )
    plugin_fetches = _plugin_fetch_requests(source, resolved_ref, required_plugins)
    if agent_profile_name is None and manifest.runtime.agent_profile is not None:
        agent_profile_name = manifest.runtime.agent_profile.default_name

    resolved_runtime = {
        "kind": "plugin_spec",
        "runner": manifest.entrypoint.runner,
        "plugin": source.model_dump(exclude_none=True),
        "plugin_resolved_ref": resolved_ref,
        "entrypoint": entrypoint_for_manifest(manifest),
        "manifest": manifest.model_dump(mode="json"),
        "required_plugins": required_plugins,
        "agent_profile_id": agent_profile_id,
        "agent_profile_name": agent_profile_name,
        "llm_profile_name": llm_profile_name,
    }

    if manifest.entrypoint.runner == "conversation":
        tarball = _build_stock_runner_tarball(
            source=source,
            resolved_ref=resolved_ref,
            manifest=manifest,
            config=config,
            resolved_runtime=resolved_runtime,
            required_plugins=required_plugins,
        )
    else:
        tarball = _build_runtime_metadata_tarball(
            manifest=manifest,
            config=config,
            resolved_runtime=resolved_runtime,
            required_plugins=required_plugins,
        )
    work_dir = work_dir.rstrip("/") or "/workspace/project"
    env = {
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
    return tarball, env, resolved_runtime, plugin_fetches


def _build_runtime_metadata_tarball(
    *,
    manifest: AutomationPluginManifest,
    config: dict[str, Any],
    resolved_runtime: dict[str, Any],
    required_plugins: list[dict[str, Any]],
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_json(tar, f"{AUTOMATION_DIR}/config.json", config)
        _add_json(tar, f"{AUTOMATION_DIR}/execution.json", resolved_runtime)
        _add_json(tar, f"{AUTOMATION_DIR}/plugins.json", required_plugins)
        if manifest.entrypoint.setup_script:
            setup_script = manifest.entrypoint.setup_script
            wrapper = (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f'bash "$AUTOMATION_PLUGIN_ROOT/{setup_script}"\n'
            )
            _add_bytes(tar, "setup.sh", wrapper.encode(), mode=0o755)
    buf.seek(0)
    return buf.read()


def _add_json(tar: tarfile.TarFile, name: str, data: Any) -> None:
    _add_bytes(tar, name, (json.dumps(data, indent=2) + "\n").encode())


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def _load_plugin_spec_preset_files() -> dict[str, str]:
    global _PLUGIN_SPEC_PRESET_CACHE
    if _PLUGIN_SPEC_PRESET_CACHE is None:
        _PLUGIN_SPEC_PRESET_CACHE = {
            "main.py": (PLUGIN_SPEC_PRESET_DIR / "sdk_main.py").read_text(),
            "prompt_builder.py": (
                PLUGIN_SPEC_PRESET_DIR / "prompt_builder.py"
            ).read_text(),
            "setup.sh": (PLUGIN_SPEC_PRESET_DIR / "setup.sh").read_text(),
        }
    return _PLUGIN_SPEC_PRESET_CACHE


def _preset_entrypoint() -> str:
    python_path = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    return f"{python_path} main.py"


def setup_script_for_manifest(manifest: AutomationPluginManifest) -> str | None:
    if manifest.entrypoint.runner == "conversation":
        return "setup.sh"
    return manifest.entrypoint.setup_script


def entrypoint_for_manifest(manifest: AutomationPluginManifest) -> str:
    if manifest.entrypoint.runner == "conversation":
        return _preset_entrypoint()
    if manifest.entrypoint.working_dir in ("", "."):
        return f'cd "$AUTOMATION_PLUGIN_ROOT" && {manifest.entrypoint.command}'
    return (
        f'cd "$AUTOMATION_PLUGIN_ROOT/{manifest.entrypoint.working_dir}" '
        f"&& {manifest.entrypoint.command}"
    )


def _conversation_prompt_for_manifest(manifest: AutomationPluginManifest) -> str:
    prompt = f"/{manifest.name}:{manifest.entrypoint.command}"
    if manifest.entrypoint.arguments:
        prompt = f"{prompt} {manifest.entrypoint.arguments}"
    return prompt


def _resolved_plugin_source(
    plugin: AutomationPluginSource, resolved_ref: str | None
) -> dict[str, Any]:
    data = plugin.model_dump(exclude_none=True)
    if resolved_ref:
        data["ref"] = resolved_ref
    return data


def _plugin_config_sources(
    source: AutomationPluginSource,
    resolved_ref: str | None,
    required_plugins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sources = [_resolved_plugin_source(source, resolved_ref)]
    for plugin in required_plugins:
        item = {k: v for k, v in plugin.items() if k != "resolved_ref"}
        if resolved_ref := plugin.get("resolved_ref"):
            item["ref"] = resolved_ref
        sources.append(item)
    return sources


def _plugin_fetch_requests(
    source: AutomationPluginSource,
    resolved_ref: str | None,
    required_plugins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _plugin_config_sources(source, resolved_ref, required_plugins)


def _build_stock_runner_tarball(
    *,
    source: AutomationPluginSource,
    resolved_ref: str | None,
    manifest: AutomationPluginManifest,
    config: dict[str, Any],
    resolved_runtime: dict[str, Any],
    required_plugins: list[dict[str, Any]],
) -> bytes:
    preset_files = _load_plugin_spec_preset_files()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bytes(tar, "main.py", preset_files["main.py"].encode())
        _add_bytes(tar, "prompt_builder.py", preset_files["prompt_builder.py"].encode())
        _add_bytes(tar, "setup.sh", preset_files["setup.sh"].encode(), mode=0o755)
        _add_bytes(
            tar, "prompt.txt", _conversation_prompt_for_manifest(manifest).encode()
        )
        _add_json(
            tar,
            "plugins_config.json",
            _plugin_config_sources(source, resolved_ref, required_plugins),
        )
        _add_json(tar, f"{AUTOMATION_DIR}/config.json", config)
        _add_json(tar, f"{AUTOMATION_DIR}/execution.json", resolved_runtime)
        _add_json(tar, f"{AUTOMATION_DIR}/plugins.json", required_plugins)
    buf.seek(0)
    return buf.read()

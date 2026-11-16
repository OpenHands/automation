"""Executable automation plugin specification support."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal
from urllib.parse import urlparse

import httpx
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
    ValidationError,
    field_validator,
    model_validator,
)

from openhands.automation.observability_associations import (
    validate_observability_associations as validate_observability_association_exprs,
)
from openhands.automation.schemas import (
    RUN_AS_USER_ID_DESCRIPTION,
    CronTrigger,
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
from openhands.sdk.secret import SecretValue


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
_FORM_TEMPLATE_RE = re.compile(
    r"{{\s*(?P<json>json\s+)?form\.(?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}"
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


# Resolves secret names to the references a plugin fetch expands its
# placeholders from; see ``plugin_secrets``.
PluginSecretLookup = Callable[[Collection[str]], Awaitable[Mapping[str, SecretValue]]]


async def no_plugin_secrets(
    names: Collection[str],  # noqa: ARG001
) -> Mapping[str, SecretValue]:
    """The lookup of a fetch that resolves no credentials."""
    return {}


# A placeholder is the whole user name or password of an http(s) URL: the SDK
# expands it, from the secrets a fetch is given, only for that fetch.
_SECRET_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CREDENTIALS_MESSAGE = (
    "plugin source URLs must not include credentials; name a secret with a "
    "placeholder in the URL user info instead, for example "
    "https://x-access-token:${GITHUB_TOKEN}@github.com/org/repo.git"
)
_PLACEHOLDER_MESSAGE = (
    "a ${NAME} placeholder is only allowed as the user name or password of an "
    "http(s) plugin source URL"
)


def _url_userinfo_secret_names(source: str) -> list[str]:
    """Secret names in the user info of an http(s) source, validating its form.

    Raises:
        ValueError: when the user info holds a literal credential, or a
            placeholder appears anywhere else in the URL.
    """
    parsed = urlparse(source)
    userinfo, has_userinfo, hostport = parsed.netloc.rpartition("@")
    rest = source.split(parsed.netloc, 1)[-1] if parsed.netloc else source
    if "${" in hostport or "${" in rest:
        raise ValueError(_PLACEHOLDER_MESSAGE)
    if not has_userinfo:
        return []
    username, has_password, password = userinfo.partition(":")
    names: list[str] = []
    if has_password:
        # The password must be a placeholder; the user name may be literal
        # (x-access-token, oauth2, a Data Center user name) or one too.
        password_match = _SECRET_PLACEHOLDER_RE.fullmatch(password)
        if password_match is None:
            raise ValueError(_CREDENTIALS_MESSAGE)
        names.append(password_match.group(1))
        if "${" in username:
            username_match = _SECRET_PLACEHOLDER_RE.fullmatch(username)
            if username_match is None:
                raise ValueError(_PLACEHOLDER_MESSAGE)
            names.append(username_match.group(1))
    else:
        # A lone user name is the credential itself (a token as user name).
        username_match = _SECRET_PLACEHOLDER_RE.fullmatch(username)
        if username_match is None:
            raise ValueError(_CREDENTIALS_MESSAGE)
        names.append(username_match.group(1))
    return names


def plugin_source_secret_names(source: str) -> list[str]:
    """The secret names a validated plugin source references, sorted."""
    parsed = urlparse(source.strip())
    if parsed.scheme not in {"http", "https"}:
        return []
    return sorted(set(_url_userinfo_secret_names(source.strip())))


def plugin_secret_names(
    plugins: Iterable[AutomationPluginSource | Mapping[str, Any]],
) -> list[str]:
    """The secret names any of ``plugins`` references, sorted."""
    names: set[str] = set()
    for plugin in plugins:
        source = (
            plugin.source
            if isinstance(plugin, AutomationPluginSource)
            else plugin.get("source")
        )
        if isinstance(source, str):
            names.update(plugin_source_secret_names(source))
    return sorted(names)


class AutomationPluginSource(BaseModel):
    """Source of an executable automation plugin or secondary plugin.

    A private repository names its credential with a ``${NAME}`` placeholder
    as the URL user name or password, for example
    ``https://x-access-token:${GITHUB_TOKEN}@github.com/org/repo.git``. The
    source is stored as written; each fetch resolves ``NAME`` for the identity
    the automation runs as. Literal credentials are rejected.
    """

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
        if source_type == SourceType.GITHUB and (
            "@" in shorthand_path or ":" in shorthand_path or "${" in shorthand_path
        ):
            raise ValueError(_CREDENTIALS_MESSAGE)
        if parsed.scheme in {"http", "https"}:
            _url_userinfo_secret_names(parsed_source)
        elif "${" in source:
            raise ValueError(_PLACEHOLDER_MESSAGE)
        return source

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is not None and "${" in value:
            raise ValueError("plugin ref must not contain a ${NAME} placeholder")
        return value

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
    bundle_files: dict[str, str] = Field(default_factory=dict)
    bundle_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Catalog-only: a template mapping rendered against the validated "
            "form config and written as config.json at the tarball root. "
            "Templates use {{form.<arg>}} syntax."
        ),
    )

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
            if self.bundle_files or self.bundle_config:
                if self.working_dir not in ("", "."):
                    raise ValueError(
                        "entrypoint.bundle_files/bundle_config are self-contained "
                        "and cannot be combined with working_dir"
                    )
                if self.setup_script is not None:
                    raise ValueError(
                        "entrypoint.bundle_files/bundle_config cannot be combined "
                        "with setup_script"
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

    run_as_user_id: uuid.UUID | None = Field(
        default=None, description=RUN_AS_USER_ID_DESCRIPTION
    )
    name: str = Field(..., min_length=1, max_length=500)
    factory_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Factory to create the automation in. Requires manage_automations "
            "(members get 403); an unknown or other-org factory returns 404."
        ),
    )
    plugin: AutomationPluginSource
    config: dict[str, Any] = Field(default_factory=dict)
    trigger: Trigger | None = None
    agent_profile_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Agent profile the runs launch from, within the manifest's "
            "runtime.agent_profile allowlists. Defaults to the profile named by "
            "its default_name."
        ),
    )
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


class PluginSpecPreflightResponse(BaseModel):
    """Read-only preflight of an executable plugin spec.

    The parsed manifest (config schema, runtime profile requirements,
    suggested trigger) plus the resolved ref, with no side effects. Used by
    launch flows to render typed config inputs and a default trigger before
    the user commits to creating an automation.
    """

    model_config = ConfigDict(extra="forbid")

    manifest: AutomationPluginManifest
    resolved_ref: str | None = None
    suggested_trigger: Trigger | None = None


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

    # Any HTTPS host: a plugin can live on a self-hosted provider such as
    # Bitbucket Data Center, not only on github.com.
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PluginSpecError(
            "Cloud plugin sources must use github:owner/repo or an https:// git URL"
        )


COMMIT_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")


def require_pinned_plugin_refs(plugins: list[PluginSource]) -> None:
    """Reject plugin refs that can move, for an automation run as a service account.

    The generated tarball stores the refs as given and the sandbox resolves them
    on every run, so a branch, tag or missing ref would let anyone who can push
    to the plugin repo change code that runs as the service account.

    Raises:
        PluginSpecError: when a ref is not a full 40-character commit SHA.
    """
    for plugin in plugins:
        if plugin.ref is None or not COMMIT_SHA_RE.fullmatch(plugin.ref):
            raise PluginSpecError(
                f"Plugin {plugin.source!r} must set ref to a full 40-character "
                "commit SHA for an automation that runs as a service account"
            )


async def pin_plugin_refs(
    plugins: list[dict[str, Any]],
    *,
    allow_local_sources: bool,
    secrets: PluginSecretLookup = no_plugin_secrets,
) -> list[dict[str, Any]]:
    """Return ``plugins`` with each ref replaced by its commit SHA.

    Used for an automation that runs as a service account (see
    ``require_pinned_plugin_refs``). A full SHA is kept as is; a branch, tag or
    missing ref is resolved by preflight, with ``secrets`` for a private source.

    Raises:
        PluginSpecError: when a source is not allowed, a plugin cannot be
            preflighted, or a ref does not resolve to a full commit SHA.
    """
    pinned = []
    for item in plugins:
        if COMMIT_SHA_RE.fullmatch(item.get("ref") or ""):
            pinned.append(item)
            continue
        try:
            source = AutomationPluginSource.model_validate(item)
        except ValidationError as exc:
            # The SDK's PluginSource (preset bodies) accepts more than this.
            raise PluginSpecError(f"Invalid plugin source: {exc}") from exc
        [resolved] = validate_required_plugins(
            [source],
            allow_local_sources=allow_local_sources,
            secrets=await secrets(plugin_secret_names([source])),
        )
        ref = resolved.pop("resolved_ref", None)
        pinned.append({**resolved, **({"ref": ref} if ref else {})})
    require_pinned_plugin_refs([PluginSource.model_validate(item) for item in pinned])
    return pinned


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


def _source_secrets(
    plugin: AutomationPluginSource, secrets: Mapping[str, SecretValue] | None
) -> dict[str, SecretValue] | None:
    """The secrets ``plugin``'s own fetch may expand: only the names it uses."""
    names = plugin_source_secret_names(plugin.source)
    if not names or not secrets:
        return None
    return {name: secrets[name] for name in names if name in secrets}


def plugin_fetch_error(
    prefix: str, exc: Exception, plugin: AutomationPluginSource
) -> PluginSpecError:
    """A ``PluginSpecError`` for a failed strict fetch of ``plugin``.

    Keeps the SDK's ``code`` and ``status_code``, so a failure is permanent or
    transient as the SDK classifies it, and names the secrets the source uses.
    A secret lookup that fails inside the fetch raises an ``httpx`` error: a
    missing secret is permanent, an unreachable lookup transient.
    """
    names = ", ".join(plugin_source_secret_names(plugin.source))
    if isinstance(exc, PluginSourceError):
        message = f"{prefix}: {exc}"
        code, status_code = exc.code, exc.status_code
        if names and code == "auth_failed":
            message += f" (the git host rejected the credentials from secret {names})"
        elif code == "source_unavailable" and "url rejected" in str(exc).lower():
            # Git refused a malformed URL, which retrying cannot fix. A
            # secret value with "/", "@" or ":" breaks the URL it is put in.
            code, status_code = "invalid_source", 422
            if names:
                message += (
                    f" (the value of secret {names} may contain a character that "
                    'is not allowed in a URL, such as "/", "@" or ":")'
                )
        return PluginSpecError(message, code=code, status_code=status_code)
    if isinstance(exc, httpx.HTTPStatusError):
        lookup_status = exc.response.status_code
        if lookup_status == 404:
            return PluginSpecError(
                f"{prefix}: Missing secret: {names}",
                code="missing_secret",
                status_code=422,
            )
        if lookup_status in (408, 429) or lookup_status >= 500:
            return PluginSpecError(
                f"{prefix}: could not read secret {names} (HTTP {lookup_status})",
                code="secret_unavailable",
                status_code=lookup_status,
            )
        return PluginSpecError(
            f"{prefix}: reading secret {names} was refused (HTTP {lookup_status})",
            code="secret_lookup_failed",
            status_code=lookup_status,
        )
    if isinstance(exc, httpx.RequestError):
        return PluginSpecError(
            f"{prefix}: could not reach the secret store for secret {names} "
            f"({type(exc).__name__})",
            code="secret_unavailable",
            status_code=503,
        )
    return PluginSpecError(f"{prefix}: {exc}")


def preflight_manifest(
    plugin: AutomationPluginSource,
    *,
    allow_local_sources: bool = True,
    secrets: Mapping[str, SecretValue] | None = None,
) -> tuple[str | None, AutomationPluginManifest]:
    """Strictly resolve a plugin source and parse its automation metadata.

    Supports three manifest formats found at the resolved plugin path:
    .plugin/plugin.json with an ``automation`` key, a legacy
    automation-plugin.json, and a catalog manifest.json (with a ``setup``
    key) whose setup.form.args are converted to a JSON Schema config_schema.
    ``secrets`` resolves the placeholders of a private source.
    """
    validate_plugin_source_allowed(plugin, allow_local_sources=allow_local_sources)
    try:
        try:
            with preflight_plugin(
                source=plugin.source,
                ref=plugin.ref,
                repo_path=plugin.repo_path,
                secrets=_source_secrets(plugin, secrets),
                allow_local=allow_local_sources,
            ) as checked:
                if checked.manifest.get(PLUGIN_AUTOMATION_KEY) is not None:
                    manifest = _read_manifest_from_plugin_metadata(checked.manifest)
                else:
                    manifest = _read_manifest(checked.path)
                _validate_manifest_paths(checked.path, manifest)
                return checked.resolved_ref, manifest
        except PluginSourceError as exc:
            if exc.code != "invalid_plugin":
                raise
            # Only fall through to the catalog path when the plugin has no
            # plugin.json (catalog manifests use manifest.json instead). Other
            # invalid_plugin errors — bad JSON, non-object manifest, Plugin.load
            # failures — indicate a broken plugin and must surface, not silently
            # fall back to a catalog checkout that may accept invalid metadata.
            if "plugin.json not found" not in exc.message:
                raise
            # Catalog plugins (no .plugin/plugin.json) fall through to here.
            # Use a throwaway temp dir for the checkout — the preflight is a
            # read-only operation that only reads the manifest, so it should
            # not write to the user persistence dir (which may be unset or
            # unwritable in containers where HOME is not writable).
            with tempfile.TemporaryDirectory(
                prefix="automation-preflight-"
            ) as tmp_cache:
                plugin_path, resolved_ref = fetch_plugin_with_resolution(
                    source=plugin.source,
                    cache_dir=Path(tmp_cache),
                    ref=plugin.ref,
                    repo_path=plugin.repo_path,
                )
                manifest = _read_manifest(plugin_path)
                _validate_manifest_paths(plugin_path, manifest)
                _validate_bundle_file_sources(plugin, resolved_ref, manifest)
                return resolved_ref, manifest
    except PluginSpecError:
        raise
    except Exception as exc:
        raise plugin_fetch_error(
            "Failed to preflight automation plugin", exc, plugin
        ) from exc


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
    if legacy_manifest_path.is_file():
        try:
            return AutomationPluginManifest.model_validate_json(
                legacy_manifest_path.read_text()
            )
        except Exception as exc:
            raise PluginSpecError(f"Invalid automation plugin manifest: {exc}") from exc

    catalog_manifest_path = _contained_path(plugin_path, "manifest.json")
    if catalog_manifest_path.is_file():
        try:
            catalog = json.loads(catalog_manifest_path.read_text())
        except Exception as exc:
            raise PluginSpecError(
                f"Invalid catalog manifest: manifest.json: {exc}"
            ) from exc
        if isinstance(catalog.get("setup"), dict):
            return _convert_catalog_manifest(catalog)

    raise PluginSpecError(
        "Automation plugin manifest not found: "
        f"{PLUGIN_MANIFEST_NAME} with `{PLUGIN_AUTOMATION_KEY}`, "
        f"{MANIFEST_NAME}, or a catalog manifest.json with a `setup` key"
    )


_CATALOG_FIELD_TYPE_MAP: Final[dict[str, str]] = {
    "textarea": "string",
    "text": "string",
    "string": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "checkbox": "boolean",
    "select": "string",
    "enum": "string",
    "llm-profile": "string",
    "repo-picker": "string",
}


def _convert_catalog_arg_to_schema(arg: dict[str, Any]) -> dict[str, Any]:
    """Convert a catalog setup.form.args entry to a JSON Schema property."""
    catalog_type = arg.get("type", "string")
    if catalog_type not in _CATALOG_FIELD_TYPE_MAP:
        raise PluginSpecError(
            f"Unknown catalog field type: {catalog_type!r}. "
            f"Supported: {', '.join(sorted(_CATALOG_FIELD_TYPE_MAP))}"
        )

    item_type = _CATALOG_FIELD_TYPE_MAP[catalog_type]
    is_multiple = bool(arg.get("multiple"))
    if is_multiple:
        schema: dict[str, Any] = {"type": "array", "items": {"type": item_type}}
    else:
        schema = {"type": item_type}

    if catalog_type == "textarea":
        schema["format"] = "textarea"
    elif catalog_type == "timezone":
        schema["format"] = "timezone"
    elif catalog_type in ("select", "enum") and arg.get("options"):
        options = _catalog_options(arg["options"])
        if is_multiple:
            schema["items"]["enum"] = options
        else:
            schema["enum"] = options

    if arg.get("label"):
        schema["title"] = arg["label"]
    if arg.get("help"):
        schema["description"] = arg["help"]
    if "default" in arg:
        schema["default"] = arg["default"]

    constraints = arg.get("constraints") or {}
    if "minLength" in constraints:
        schema["minLength"] = constraints["minLength"]
    if "maxLength" in constraints:
        schema["maxLength"] = constraints["maxLength"]
    if "minimum" in constraints:
        schema["minimum"] = constraints["minimum"]
    elif "min" in constraints:
        schema["minimum"] = constraints["min"]
    if "maximum" in constraints:
        schema["maximum"] = constraints["maximum"]
    elif "max" in constraints:
        schema["maximum"] = constraints["max"]
    if "pattern" in constraints:
        schema["pattern"] = constraints["pattern"]
    if is_multiple and arg.get("required"):
        schema["minItems"] = 1

    if (
        arg.get("options")
        and "enum" not in schema
        and "enum" not in schema.get("items", {})
    ):
        options = _catalog_options(arg["options"])
        if is_multiple:
            schema["items"]["enum"] = options
        else:
            schema["enum"] = options

    return schema


def _catalog_options(options: list[Any]) -> list[Any]:
    return [opt.get("value", opt) if isinstance(opt, dict) else opt for opt in options]


def _convert_catalog_manifest(catalog: dict[str, Any]) -> AutomationPluginManifest:
    """Convert an automations/catalog manifest to an AutomationPluginManifest."""
    setup = catalog.get("setup") or {}
    form = setup.get("form") or {}
    args = form.get("args") or {}
    triggers = form.get("triggers") or {}
    bundle = setup.get("bundle") or {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, arg in args.items():
        if not isinstance(arg, dict):
            continue
        properties[name] = _convert_catalog_arg_to_schema(arg)
        if arg.get("required"):
            required.append(name)

    config_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        config_schema["required"] = required

    suggested_trigger: Trigger | None = None
    cron = triggers.get("cron") or {}
    schedule = cron.get("schedule") or {}
    if isinstance(schedule, dict) and schedule.get("default"):
        tz = cron.get("timezone") or {}
        suggested_trigger = CronTrigger(
            type="cron",
            schedule=schedule["default"],
            timezone=tz.get("default", "UTC") if isinstance(tz, dict) else "UTC",
        )

    entrypoint_command = bundle.get("entrypoint")
    bundle_files = bundle.get("files") or {}
    bundle_config = bundle.get("config") or None
    if entrypoint_command is None:
        raise PluginSpecError(
            "Catalog manifest setup.bundle.entrypoint is required: catalog "
            "plugins have no .plugin/plugin.json, so without an explicit "
            "entrypoint the worker would run 'python3 main.py' against a "
            "file that does not exist. Define setup.bundle.entrypoint "
            "(and setup.bundle.files) in the manifest."
        )
    if bundle_config is not None and not isinstance(bundle_config, dict):
        raise PluginSpecError("Catalog manifest setup.bundle.config must be an object")
    try:
        entrypoint = AutomationPluginEntrypoint(
            runner="script",
            command=entrypoint_command,
            working_dir=".",
            bundle_files=bundle_files,
            bundle_config=bundle_config,
        )
    except Exception as exc:
        raise PluginSpecError(
            f"Catalog manifest has invalid entrypoint: {exc}"
        ) from exc

    description = catalog.get("description")
    if description and len(description) > 500:
        description = description[:497] + "..."

    data = {
        "schema_version": "openhands.automation.plugin.v1",
        "name": catalog.get("name") or catalog.get("id") or "catalog-automation",
        "description": description,
        "entrypoint": entrypoint.model_dump(),
        "config_schema": config_schema,
        "suggested_trigger": suggested_trigger.model_dump()
        if suggested_trigger
        else None,
    }

    try:
        return AutomationPluginManifest.model_validate(data)
    except Exception as exc:
        raise PluginSpecError(f"Failed to convert catalog manifest: {exc}") from exc


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
    if manifest.entrypoint.runner == "conversation":
        conversation_prompt(manifest, plugin_config)
    return plugin_config


def _config_value(config: dict[str, Any], path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise PluginSpecError(
                f"Template references missing config value: config.{path}"
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


def _render_form_templates(value: Any, config: dict[str, Any]) -> Any:
    """Render {{form.<path>}} templates against validated plugin config.

    Catalog bundle.config maps form arg names to config file keys, e.g.
    ``{"repos": "{{form.repositories}}"}}``. The config dict is keyed by the
    form arg name, so ``form.repositories`` resolves to ``config["repositories"]``.

    When the whole string is a single template (``"{{form.x}}"``), the resolved
    value is substituted as-is so it keeps its JSON type — a list stays a list,
    a number stays a number. When the template is embedded in a longer string,
    it is stringified so it can be interpolated.
    """
    if isinstance(value, dict):
        return {
            key: _render_form_templates(item, config) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_render_form_templates(item, config) for item in value]
    if not isinstance(value, str):
        return value

    full = _FORM_TEMPLATE_RE.fullmatch(value.strip())
    if full is not None and full.group(0) == value:
        resolved = _config_value(config, full.group("path"))
        if full.group("json"):
            return json.dumps(resolved)
        return resolved

    def replace(match: re.Match[str]) -> str:
        resolved = _config_value(config, match.group("path"))
        if match.group("json"):
            return json.dumps(resolved)
        return str(resolved)

    return _FORM_TEMPLATE_RE.sub(replace, value)


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
    secrets: Mapping[str, SecretValue] | None = None,
) -> list[dict[str, Any]]:
    """Preflight secondary plugins and return source specs with resolved refs.

    ``secrets`` resolves the placeholders of private sources.
    """
    resolved: list[dict[str, Any]] = []
    for plugin in plugins:
        validate_plugin_source_allowed(plugin, allow_local_sources=allow_local_sources)
        try:
            with preflight_plugin(
                source=plugin.source,
                ref=plugin.ref,
                repo_path=plugin.repo_path,
                secrets=_source_secrets(plugin, secrets),
                allow_local=allow_local_sources,
            ) as checked:
                resolved_ref = checked.resolved_ref
        except Exception as exc:
            raise plugin_fetch_error(
                f"Failed to preflight required plugin {plugin.source!r}", exc, plugin
            ) from exc
        item = plugin.model_dump(exclude_none=True)
        if resolved_ref:
            item["resolved_ref"] = resolved_ref
        resolved.append(item)
    return resolved


async def build_execution_payload(
    *,
    plugin_spec: dict[str, Any],
    agent_profile_id: str | None,
    agent_profile_name: str | None,
    llm_profile_name: str | None,
    work_dir: str,
    allow_local_sources: bool = True,
    secrets: PluginSecretLookup = no_plugin_secrets,
) -> tuple[bytes, dict[str, str], dict[str, Any], list[dict[str, Any]]]:
    """Preflight, validate, and package a plugin-backed automation run.

    ``secrets`` resolves the placeholders of private sources, for the
    identity the automation runs as.
    """
    source = AutomationPluginSource.model_validate(plugin_spec["plugin"])
    requested_config = plugin_spec.get("config") or {}
    resolved_ref, manifest = preflight_manifest(
        source,
        allow_local_sources=allow_local_sources,
        secrets=await secrets(plugin_secret_names([source])),
    )
    config = validate_plugin_config(manifest, requested_config)
    # A stored list pins the secondary plugins (automations that run as a
    # service account); otherwise they follow the manifest's refs.
    stored_required = plugin_spec.get("required_plugins")
    required_sources = (
        [AutomationPluginSource.model_validate(item) for item in stored_required]
        if stored_required is not None
        else manifest.runtime.plugins
    )
    required_plugins = validate_required_plugins(
        required_sources,
        allow_local_sources=allow_local_sources,
        secrets=await secrets(plugin_secret_names(required_sources)),
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

    bundle_file_data: dict[str, bytes] | None = None
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
        if manifest.entrypoint.bundle_files:
            bundle_file_data = _fetch_bundle_files(
                source, resolved_ref, manifest.entrypoint.bundle_files
            )
            # Catalog plugins with bundle files are self-contained — the
            # tarball carries the script. Skip the agent-server pre-fetch for
            # the main plugin (which rejects catalog plugins for lacking
            # .plugin/plugin.json) and only fetch declared runtime plugins.
            plugin_fetches = _plugin_fetch_requests(
                source=None,
                resolved_ref=resolved_ref,
                required_plugins=required_plugins,
            )
        tarball = _build_runtime_metadata_tarball(
            manifest=manifest,
            config=config,
            resolved_runtime=resolved_runtime,
            required_plugins=required_plugins,
            bundle_files=bundle_file_data,
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


def _validate_bundle_file_sources(
    source: AutomationPluginSource,
    resolved_ref: str | None,
    manifest: AutomationPluginManifest,
) -> None:
    """Check that declared bundle file sources exist in the repo.

    Catalog manifests declare ``setup.bundle.files`` as destination → source
    paths relative to the repo root. A typo in a source path would otherwise
    sail through preflight and create, failing only at dispatch when the
    tarball is assembled. Sources are validated here against a full checkout
    (no ``repo_path``) so the error surfaces at preflight time.
    """
    bundle_files = manifest.entrypoint.bundle_files
    if not bundle_files:
        return
    with tempfile.TemporaryDirectory(prefix="automation-bundle-check-") as tmp_cache:
        try:
            repo_root, _ = fetch_plugin_with_resolution(
                source=source.source,
                cache_dir=Path(tmp_cache),
                ref=resolved_ref or source.ref,
                repo_path=None,
            )
        except Exception as exc:
            raise PluginSpecError(
                f"Failed to fetch plugin repo for bundle file check: {exc}"
            ) from exc
        for dest, src_path in bundle_files.items():
            if not isinstance(src_path, str) or not src_path.strip():
                raise PluginSpecError(
                    f"Bundle file source for '{dest}' must be a non-empty string"
                )
            candidate = repo_root / src_path.strip("/")
            try:
                candidate.resolve().relative_to(repo_root.resolve())
            except ValueError as exc:
                raise PluginSpecError(
                    f"Bundle file source escapes repo root: {src_path}"
                ) from exc
            if not candidate.is_file():
                raise PluginSpecError(f"Bundle file not found in repo: {src_path}")


def _fetch_bundle_files(
    source: AutomationPluginSource,
    resolved_ref: str | None,
    bundle_files: dict[str, str],
) -> dict[str, bytes]:
    """Fetch the full plugin repo and read bundle file contents.

    Catalog manifests declare ``setup.bundle.files`` as a mapping of
    tarball-destination → repo-relative-source. Source paths are relative to
    the repository root, so the repo is fetched *without* ``repo_path`` (the
    full clone) even when the plugin source uses a subdirectory.
    """
    if not bundle_files:
        return {}
    with tempfile.TemporaryDirectory(prefix="automation-bundle-") as tmp_cache:
        try:
            repo_root, _ = fetch_plugin_with_resolution(
                source=source.source,
                cache_dir=Path(tmp_cache),
                ref=resolved_ref or source.ref,
                repo_path=None,
            )
        except Exception as exc:
            raise PluginSpecError(
                f"Failed to fetch plugin repo for bundle files: {exc}"
            ) from exc

        result: dict[str, bytes] = {}
        for dest, src_path in bundle_files.items():
            if not isinstance(src_path, str) or not src_path.strip():
                raise PluginSpecError(
                    f"Bundle file source for '{dest}' must be a non-empty string"
                )
            candidate = repo_root / src_path.strip("/")
            try:
                candidate.resolve().relative_to(repo_root.resolve())
            except ValueError as exc:
                raise PluginSpecError(
                    f"Bundle file source escapes repo root: {src_path}"
                ) from exc
            if not candidate.is_file():
                raise PluginSpecError(f"Bundle file not found in repo: {src_path}")
            result[dest] = candidate.read_bytes()
        return result


def _build_runtime_metadata_tarball(
    *,
    manifest: AutomationPluginManifest,
    config: dict[str, Any],
    resolved_runtime: dict[str, Any],
    required_plugins: list[dict[str, Any]],
    bundle_files: dict[str, bytes] | None = None,
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
        if manifest.entrypoint.bundle_config:
            rendered = _render_form_templates(manifest.entrypoint.bundle_config, config)
            _add_json(tar, "config.json", rendered)
        if bundle_files:
            for dest, data in bundle_files.items():
                _safe_add_tar_path(tar, dest, data)
    buf.seek(0)
    return buf.read()


_RESERVED_BUNDLE_DESTINATIONS: Final[frozenset[str]] = frozenset(
    {
        "config.json",
        f"{AUTOMATION_DIR}/config.json",
        f"{AUTOMATION_DIR}/execution.json",
        f"{AUTOMATION_DIR}/plugins.json",
        "setup.sh",
    }
)


def _safe_add_tar_path(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add a file to the tarball, rejecting path escapes and reserved names."""
    if name.startswith("/") or ".." in PurePosixPath(name).parts:
        raise PluginSpecError(f"Bundle file path escapes tarball root: {name}")
    if name in _RESERVED_BUNDLE_DESTINATIONS:
        raise PluginSpecError(f"Bundle file destination is reserved: {name}")
    _add_bytes(tar, name, data, mode=0o755 if name.endswith(".sh") else 0o644)


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
            "setup.py": (PRESETS_DIR / "setup.py").read_text(),
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
    if manifest.entrypoint.bundle_files:
        return manifest.entrypoint.command
    if manifest.entrypoint.working_dir in ("", "."):
        return f'cd "$AUTOMATION_PLUGIN_ROOT" && {manifest.entrypoint.command}'
    return (
        f'cd "$AUTOMATION_PLUGIN_ROOT/{manifest.entrypoint.working_dir}" '
        f"&& {manifest.entrypoint.command}"
    )


def conversation_prompt(
    manifest: AutomationPluginManifest, config: dict[str, Any]
) -> str:
    """Return the message a conversation runner sends, arguments rendered."""
    prompt = f"/{manifest.name}:{manifest.entrypoint.command}"
    if manifest.entrypoint.arguments:
        arguments = _render_config_templates(manifest.entrypoint.arguments, config)
        prompt = f"{prompt} {arguments}"
    return prompt


def _resolved_plugin_source(
    plugin: AutomationPluginSource, resolved_ref: str | None
) -> dict[str, Any]:
    data = plugin.model_dump(exclude_none=True)
    if resolved_ref:
        data["ref"] = resolved_ref
    return data


def _plugin_config_sources(
    source: AutomationPluginSource | None,
    resolved_ref: str | None,
    required_plugins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    if source is not None:
        sources.append(_resolved_plugin_source(source, resolved_ref))
    for plugin in required_plugins:
        item = {k: v for k, v in plugin.items() if k != "resolved_ref"}
        if resolved_ref := plugin.get("resolved_ref"):
            item["ref"] = resolved_ref
        sources.append(item)
    return sources


def _plugin_fetch_requests(
    source: AutomationPluginSource | None,
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
        if os.name == "nt":
            _add_bytes(tar, "setup.py", preset_files["setup.py"].encode())
        _add_bytes(tar, "prompt.txt", conversation_prompt(manifest, config).encode())
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

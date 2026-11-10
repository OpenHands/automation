import json
import tarfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import openhands.automation.plugin_specs as plugin_specs
from openhands.automation.plugin_specs import (
    AutomationPluginSource,
    PluginSpecError,
    build_execution_payload,
    fetch_manifest,
    preflight_manifest,
    resolve_suggested_trigger,
    validate_plugin_config,
    validate_required_plugins,
)


def write_plugin(root: Path, *, setup_script: str | None = None) -> Path:
    plugin = root / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "main.py").write_text("print('hello')\n")
    if setup_script:
        (plugin / setup_script).parent.mkdir(parents=True, exist_ok=True)
        (plugin / setup_script).write_text("echo setup\n")
    entrypoint = {"command": "python3 scripts/main.py"}
    if setup_script:
        entrypoint["setup_script"] = setup_script
    automation = {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": entrypoint,
        "runtime": {
            "agent_profile": {
                "required": True,
                "default_name": "agent-default",
                "allowed_tools": ["terminal", "file_editor"],
            },
            "llm_profile": {"required": True, "default_name": "default"},
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"message": {"type": "string"}},
        },
    }
    (plugin / ".plugin").mkdir()
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-world-automation",
                "version": "0.1.0",
                "description": "test plugin",
                "automation": automation,
            }
        )
    )
    (plugin / "automation-plugin.json").write_text(
        json.dumps({"name": "hello-world-automation", **automation})
    )
    return plugin


def _write_automation_manifest(plugin: Path, manifest: dict) -> None:
    (plugin / "automation-plugin.json").write_text(json.dumps(manifest))
    plugin_manifest = json.loads((plugin / ".plugin" / "plugin.json").read_text())
    plugin_manifest["automation"] = {
        key: value for key, value in manifest.items() if key != "name"
    }
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(plugin_manifest))


def write_conversation_plugin(root: Path) -> Path:
    plugin = root / "conversation-plugin"
    (plugin / "commands").mkdir(parents=True)
    (plugin / "commands" / "hello.md").write_text("Say hello to $ARGUMENTS.\n")
    (plugin / ".plugin").mkdir()
    automation = {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": {
            "runner": "conversation",
            "command": "hello",
            "arguments": "world",
        },
        "runtime": {
            "agent_profile": {
                "required": True,
                "default_name": "agent-default",
                "allowed_tools": ["terminal"],
            },
        },
    }
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-world-automation",
                "version": "0.1.0",
                "description": "test plugin",
                "automation": automation,
            }
        )
    )
    (plugin / "automation-plugin.json").write_text(
        json.dumps({"name": "hello-world-automation", **automation})
    )
    return plugin


def write_merged_conversation_plugin(root: Path) -> Path:
    plugin = root / "merged-conversation-plugin"
    (plugin / "commands").mkdir(parents=True)
    (plugin / "commands" / "hello.md").write_text("Say hello.\n")
    (plugin / ".plugin").mkdir()
    (plugin / ".plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-world-automation",
                "version": "0.1.0",
                "description": "test plugin",
                "automation": {
                    "schema_version": "openhands.automation.plugin.v1",
                    "entrypoint": {"runner": "conversation", "command": "hello"},
                    "suggested_trigger": {
                        "type": "cron",
                        "schedule": "0 9 * * *",
                        "timezone": "UTC",
                    },
                    "runtime": {
                        "agent_profile": {
                            "required": True,
                            "default_name": "agent-default",
                            "allowed_tools": ["terminal"],
                        },
                    },
                },
            }
        )
    )
    return plugin


def test_plugin_source_rejects_url_credentials():
    with pytest.raises(ValidationError, match="must not include credentials"):
        AutomationPluginSource(source="https://user:pass@github.com/acme/private.git")


def test_plugin_source_rejects_github_shorthand_credentials():
    with pytest.raises(ValidationError, match="must not include credentials"):
        AutomationPluginSource(source="github:user:token@acme/private")


def test_required_plugin_source_rejects_github_shorthand_credentials(tmp_path):
    plugin = write_conversation_plugin(tmp_path)
    manifest = json.loads((plugin / "automation-plugin.json").read_text())
    manifest["runtime"]["plugins"] = [{"source": "github:user:token@acme/private"}]

    with pytest.raises(ValidationError, match="must not include credentials"):
        plugin_specs.AutomationPluginManifest.model_validate(manifest)


def test_preflight_rejects_local_source_when_local_sources_disabled(tmp_path):
    plugin = write_plugin(tmp_path)

    with pytest.raises(
        PluginSpecError, match="Local plugin sources are not allowed in cloud mode"
    ):
        preflight_manifest(
            AutomationPluginSource(source=str(plugin)),
            allow_local_sources=False,
        )


def test_preflight_rejects_file_source_when_local_sources_disabled():
    with pytest.raises(
        PluginSpecError, match="file:// plugin sources are not allowed in cloud mode"
    ):
        preflight_manifest(
            AutomationPluginSource(source="file:///tmp/private-plugin"),
            allow_local_sources=False,
        )


@pytest.mark.parametrize(
    "source",
    [
        "http://bitbucket.example.com/scm/proj/plugin.git",
        "git@github.com:acme/private.git",
    ],
)
def test_preflight_rejects_non_https_git_source_when_local_sources_disabled(source):
    with pytest.raises(
        PluginSpecError,
        match="Cloud plugin sources must use github:owner/repo or an https:// git URL",
    ):
        preflight_manifest(
            AutomationPluginSource(source=source),
            allow_local_sources=False,
        )


@pytest.mark.parametrize(
    "source",
    [
        # A Bitbucket Data Center repository, with and without a port and a
        # context path.
        "https://bitbucket.example.com/scm/proj/plugin.git",
        "https://git.example.com:8443/bitbucket/scm/proj/plugin.git",
        "https://gitlab.example.com/group/plugin.git",
        "https://github.com/acme/plugin.git",
    ],
)
def test_cloud_plugin_sources_accept_https_git_urls_on_any_host(source):
    plugin_specs.validate_plugin_source_allowed(
        AutomationPluginSource(source=source), allow_local_sources=False
    )


def test_required_plugins_reject_local_source_when_local_sources_disabled(tmp_path):
    plugin = write_plugin(tmp_path)
    required_plugin = write_plugin(tmp_path / "required")
    manifest_data = json.loads((plugin / "automation-plugin.json").read_text())
    manifest_data.setdefault("runtime", {})["plugins"] = [
        {"source": str(required_plugin)}
    ]
    _write_automation_manifest(plugin, manifest_data)
    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(plugin))
    )

    with pytest.raises(
        PluginSpecError, match="Local plugin sources are not allowed in cloud mode"
    ):
        validate_required_plugins(manifest.runtime.plugins, allow_local_sources=False)


def test_validate_plugin_config_applies_defaults(tmp_path):
    plugin = write_plugin(tmp_path)
    data = json.loads((plugin / "automation-plugin.json").read_text())
    data["config_schema"] = {
        "type": "object",
        "required": ["repository"],
        "additionalProperties": False,
        "properties": {
            "repository": {"type": "string"},
            "label": {"type": "string", "default": "ready-for-review"},
        },
    }
    _write_automation_manifest(plugin, data)

    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(plugin))
    )

    assert validate_plugin_config(manifest, {"repository": "OpenHands/automation"}) == {
        "repository": "OpenHands/automation",
        "label": "ready-for-review",
    }


def test_resolve_suggested_trigger_template_uses_normal_install_config(tmp_path):
    plugin = write_plugin(tmp_path)
    data = json.loads((plugin / "automation-plugin.json").read_text())
    data["config_schema"] = {
        "type": "object",
        "required": ["repository"],
        "properties": {
            "repository": {"type": "string"},
            "label": {"type": "string", "default": "ready-for-review"},
        },
    }
    data["suggested_trigger_template"] = {
        "type": "event",
        "source": "github",
        "on": "pull_request.labeled",
        "filter": (
            "repository.full_name == {{ json config.repository }} && "
            "contains(pull_request.labels[].name, {{ json config.label }})"
        ),
    }
    _write_automation_manifest(plugin, data)
    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(plugin))
    )
    config = validate_plugin_config(manifest, {"repository": "OpenHands/automation"})

    trigger = resolve_suggested_trigger(manifest, config)

    assert trigger is not None
    assert trigger.model_dump() == {
        "type": "event",
        "source": "github",
        "on": "pull_request.labeled",
        "filter": (
            'repository.full_name == "OpenHands/automation" && '
            'contains(pull_request.labels[].name, "ready-for-review")'
        ),
        "destination": "dispatch_run",
        "subject_key_expr": None,
        "turn_text_expr": None,
        "wake_agent": True,
    }


async def test_fetch_manifest_and_build_execution_payload(tmp_path):
    plugin = write_plugin(tmp_path, setup_script="scripts/setup.sh")

    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))
    assert manifest.name == "hello-world-automation"

    tarball, env, runtime, plugin_fetches = await build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {"message": "hi"}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name="default",
        work_dir="/tmp/automation-run",
    )

    assert plugin_fetches == [{"source": str(plugin)}]
    assert env["AUTOMATION_AGENT_PROFILE_ID"] == "profile-id"
    assert env["AUTOMATION_AGENT_PROFILE_NAME"] == "agent-default"
    assert env["AUTOMATION_LLM_PROFILE_NAME"] == "default"
    assert (
        env["AUTOMATION_CONFIG_PATH"] == "/tmp/automation-run/.automation/config.json"
    )
    assert (
        runtime["entrypoint"]
        == 'cd "$AUTOMATION_PLUGIN_ROOT" && python3 scripts/main.py'
    )
    assert runtime["manifest"]["runtime"]["agent_profile"]["allowed_tools"] == [
        "terminal",
        "file_editor",
    ]

    assert runtime["manifest"]["name"] == "hello-world-automation"

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "automation-plugin.json" not in names
        assert "scripts/main.py" not in names
        assert "scripts/setup.sh" not in names
        assert "setup.sh" in names
        config_file = tar.extractfile(".automation/config.json")
        execution_file = tar.extractfile(".automation/execution.json")
        assert config_file is not None
        assert execution_file is not None
        config = json.loads(config_file.read())
        execution = json.loads(execution_file.read())

    assert config == {"message": "hi"}
    assert execution["agent_profile_name"] == "agent-default"


def test_fetch_manifest_rejects_missing_entrypoint(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "automation-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": "openhands.automation.plugin.v1",
                "name": "broken",
                "entrypoint": {"command": "./missing.py"},
            }
        )
    )

    with pytest.raises(PluginSpecError, match="Entrypoint target does not exist"):
        fetch_manifest(AutomationPluginSource(source=str(plugin)))


async def test_stock_conversation_runner_packages_primary_plugin_command(tmp_path):
    plugin = write_conversation_plugin(tmp_path)

    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))
    assert manifest.entrypoint.runner == "conversation"

    tarball, env, runtime, plugin_fetches = await build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {"message": "hi"}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name="default",
        work_dir="/tmp/automation-run",
    )

    assert plugin_fetches == [{"source": str(plugin)}]
    assert runtime["runner"] == "conversation"
    assert runtime["entrypoint"].endswith(" main.py")
    assert (
        env["AUTOMATION_CONFIG_PATH"] == "/tmp/automation-run/.automation/config.json"
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert {
            "main.py",
            "prompt_builder.py",
            "setup.sh",
            "prompt.txt",
            "plugins_config.json",
            ".automation/config.json",
            ".automation/execution.json",
            ".automation/plugins.json",
        }.issubset(names)
        assert "automation-plugin.json" not in names
        prompt_file = tar.extractfile("prompt.txt")
        plugins_file = tar.extractfile("plugins_config.json")
        main_file = tar.extractfile("main.py")
        assert prompt_file is not None
        assert plugins_file is not None
        assert main_file is not None
        prompt = prompt_file.read().decode()
        plugins_config = json.loads(plugins_file.read())
        main_source = main_file.read().decode()

    assert prompt == "/hello-world-automation:hello world"
    assert plugins_config == [{"source": str(plugin)}]
    assert "StartConversationRequest" in main_source
    assert "RemoteConversation.create" in main_source
    assert "get_default_agent" not in main_source
    assert "Conversation(**conversation_kwargs)" not in main_source
    assert "clone_repos" not in main_source
    assert "repos_config.json" not in main_source
    assert "AUTOMATION_LLM_PROFILE_NAME" not in main_source
    assert "AgentLaunchAdditions" not in main_source
    assert "agent_launch_additions" not in main_source
    assert "experiment_config.json" not in main_source
    assert "threading" not in main_source
    assert "conversation_title" not in main_source
    assert "report_phase" not in main_source


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_stock_conversation_runner_packages_platform_setup(tmp_path, monkeypatch, platform):
    plugin = write_conversation_plugin(tmp_path)
    source = AutomationPluginSource(source=str(plugin))
    _, _, manifest = fetch_manifest(source)
    monkeypatch.setattr(plugin_specs, "_PLUGIN_SPEC_PRESET_CACHE", None)
    monkeypatch.setattr(plugin_specs, "os", SimpleNamespace(name=platform))

    tarball = plugin_specs._build_stock_runner_tarball(
        source=source,
        resolved_ref=None,
        manifest=manifest,
        config={},
        resolved_runtime={},
        required_plugins=[],
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        assert "setup.sh" in tar.getnames()
        if platform == "nt":
            setup = tar.extractfile("setup.py")
            assert setup is not None
            assert setup.read() == (plugin_specs.PRESETS_DIR / "setup.py").read_bytes()
        else:
            assert "setup.py" not in tar.getnames()


async def test_stock_conversation_runner_uses_plugin_spec_preset(tmp_path, monkeypatch):
    plugin = write_conversation_plugin(tmp_path)
    sentinel_main = "print('plugin-spec conversation runner')\n"
    monkeypatch.setattr(
        plugin_specs,
        "_PLUGIN_SPEC_PRESET_CACHE",
        {
            "main.py": sentinel_main,
            "prompt_builder.py": (
                "def build_user_prompt(prompt, event_context):\n    return prompt\n"
            ),
            "setup.sh": "#!/usr/bin/env bash\n",
        },
    )

    tarball, _env, _runtime, _plugin_fetches = await build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin)}, "config": {}},
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name=None,
        work_dir="/tmp/automation-run",
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        main_file = tar.extractfile("main.py")
        assert main_file is not None
        assert main_file.read().decode() == sentinel_main


def test_conversation_runner_rejects_missing_plugin_command(tmp_path):
    plugin = write_conversation_plugin(tmp_path)
    (plugin / "commands" / "hello.md").unlink()

    with pytest.raises(
        PluginSpecError, match="Conversation runner command does not exist"
    ):
        fetch_manifest(AutomationPluginSource(source=str(plugin)))


def test_fetch_manifest_reads_automation_from_plugin_json(tmp_path):
    plugin = write_merged_conversation_plugin(tmp_path)

    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))

    assert manifest.name == "hello-world-automation"
    assert manifest.description == "test plugin"
    assert manifest.entrypoint.runner == "conversation"
    assert manifest.entrypoint.command == "hello"
    assert manifest.suggested_trigger is not None
    assert manifest.suggested_trigger.type == "cron"
    assert manifest.suggested_trigger.schedule == "0 9 * * *"


def test_fetch_manifest_rejects_invalid_suggested_cron(tmp_path):
    plugin = write_merged_conversation_plugin(tmp_path)
    data = json.loads((plugin / ".plugin" / "plugin.json").read_text())
    data["automation"]["suggested_trigger"]["schedule"] = "0 0 31 2 *"
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(data))

    with pytest.raises(PluginSpecError, match="Invalid automation plugin manifest"):
        fetch_manifest(AutomationPluginSource(source=str(plugin)))


def _write_catalog_manifest(root: Path) -> Path:
    """Write a catalog-style manifest.json (setup.form.args format)."""
    catalog_dir = root / "catalog-plugin"
    catalog_dir.mkdir()
    (catalog_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "test-catalog",
                "name": "Test Catalog Automation",
                "description": "A catalog entry for testing.",
                "setup": {
                    "version": "1.0",
                    "mode": "direct",
                    "form": {
                        "triggers": {
                            "cron": {
                                "schedule": {
                                    "type": "cron",
                                    "label": "Schedule",
                                    "default": "0 8 * * *",
                                    "required": True,
                                },
                                "timezone": {
                                    "type": "timezone",
                                    "label": "Timezone",
                                    "default": "UTC",
                                    "required": True,
                                },
                            }
                        },
                        "args": {
                            "feeds": {
                                "type": "textarea",
                                "label": "Feeds",
                                "help": "One feed URL per line.",
                                "default": "https://example.com/rss",
                                "required": True,
                                "constraints": {"minLength": 8, "maxLength": 4000},
                            },
                            "topics": {
                                "type": "textarea",
                                "label": "Topics of interest",
                                "help": "One topic per line.",
                                "default": "ai, open source",
                                "required": False,
                                "constraints": {"maxLength": 2000},
                            },
                        },
                    },
                    "bundle": {
                        "entrypoint": "python3 main.py",
                        "timeout": 900,
                        "files": {"main.py": "scripts/main.py"},
                        "config": {
                            "feeds": "{{form.feeds}}",
                            "topics": "{{form.topics}}",
                        },
                    },
                },
            }
        )
    )
    return catalog_dir


def _ensure_catalog_bundle_files(repo_root: Path) -> None:
    """Create the files referenced by the default bundle.files mapping."""
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    (scripts_dir / "main.py").write_text("print('hello from bundle')\n")


def test_preflight_converts_catalog_manifest(tmp_path):
    catalog_dir = _write_catalog_manifest(tmp_path)
    _ensure_catalog_bundle_files(catalog_dir)

    resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(catalog_dir)),
        allow_local_sources=True,
    )

    assert manifest.name == "Test Catalog Automation"
    assert manifest.description == "A catalog entry for testing."
    assert manifest.entrypoint.runner == "script"
    assert manifest.entrypoint.command == "python3 main.py"
    assert manifest.entrypoint.working_dir == "."

    props = manifest.config_schema["properties"]
    assert set(props.keys()) == {"feeds", "topics"}
    assert props["feeds"]["type"] == "string"
    assert props["feeds"]["format"] == "textarea"
    assert props["feeds"]["title"] == "Feeds"
    assert props["feeds"]["description"] == "One feed URL per line."
    assert props["feeds"]["default"] == "https://example.com/rss"
    assert props["feeds"]["minLength"] == 8
    assert props["feeds"]["maxLength"] == 4000
    assert props["topics"]["title"] == "Topics of interest"

    assert manifest.config_schema["required"] == ["feeds"]

    assert manifest.suggested_trigger is not None
    assert manifest.suggested_trigger.type == "cron"
    assert manifest.suggested_trigger.schedule == "0 8 * * *"
    assert manifest.suggested_trigger.timezone == "UTC"


def test_preflight_catalog_manifest_validates_config(tmp_path):
    catalog_dir = _write_catalog_manifest(tmp_path)
    _ensure_catalog_bundle_files(catalog_dir)

    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(catalog_dir)),
        allow_local_sources=True,
    )

    valid_config = validate_plugin_config(
        manifest, {"feeds": "https://example.com/rss"}
    )
    assert valid_config["feeds"] == "https://example.com/rss"

    with pytest.raises(PluginSpecError, match="invalid"):
        validate_plugin_config(manifest, {"feeds": "short"})


def test_preflight_catalog_manifest_uses_temp_dir_not_home_cache(tmp_path, monkeypatch):
    """Preflight fallback must use a throwaway temp dir, not DEFAULT_CACHE_DIR.

    Regression test for staging, where the automation container runs with
    HOME=/home/automation (not writable). The fallback fetch path used to
    pass ``_cache_dir_for_plugin(plugin)`` (rooted at ``~/.openhands``), which
    blew up with ``[Errno 13] Permission denied: '/home/automation'``. It now
    passes a ``tempfile.TemporaryDirectory`` path so the preflight never
    touches the user persistence dir.
    """
    catalog_dir = _write_catalog_manifest(tmp_path)
    _ensure_catalog_bundle_files(catalog_dir)

    captured: dict[str, object] = {}

    def _fake_fetch(*, source, cache_dir, ref, repo_path):
        captured["cache_dir"] = cache_dir
        # Return the catalog dir as the plugin path; manifest is read off it.
        return catalog_dir, None

    monkeypatch.setattr(
        "openhands.automation.plugin_specs.fetch_plugin_with_resolution", _fake_fetch
    )
    # Make preflight_plugin raise invalid_plugin so the fallback runs.
    # It's used as a context manager, so the error must fire on __enter__.
    from openhands.automation.plugin_specs import PluginSourceError

    class _InvalidPluginCM:
        def __enter__(self):
            raise PluginSourceError(
                "invalid_plugin", "Invalid plugin: plugin.json not found."
            )

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "openhands.automation.plugin_specs.preflight_plugin",
        lambda *a, **kw: _InvalidPluginCM(),
    )

    preflight_manifest(
        AutomationPluginSource(source=str(catalog_dir), ref="main"),
        allow_local_sources=True,
    )

    cache_dir = captured["cache_dir"]
    assert isinstance(cache_dir, Path)
    # The temp dir must live under the system temp root, not ~/.openhands.
    import tempfile

    assert str(cache_dir).startswith(tempfile.gettempdir())
    assert ".openhands" not in str(cache_dir)


def _write_catalog_manifest_with_bundle(root: Path) -> Path:
    """Write a catalog plugin whose bundle.files references a repo-root path.

    Returns repo_root. The repo root contains manifest.json and the actual
    script referenced by bundle.files (scripts/main.py).
    """
    repo_root = root / "repo"
    repo_root.mkdir()
    (repo_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "test-catalog",
                "name": "Test Catalog Automation",
                "description": "A catalog entry for testing.",
                "setup": {
                    "version": "1.0",
                    "mode": "direct",
                    "form": {
                        "triggers": {
                            "cron": {
                                "schedule": {
                                    "type": "cron",
                                    "default": "0 8 * * *",
                                    "required": True,
                                },
                                "timezone": {
                                    "type": "timezone",
                                    "default": "UTC",
                                    "required": True,
                                },
                            }
                        },
                        "args": {
                            "feeds": {
                                "type": "textarea",
                                "label": "Feeds",
                                "default": "https://example.com/rss",
                                "required": True,
                            }
                        },
                    },
                    "bundle": {
                        "entrypoint": "python3 main.py",
                        "timeout": 900,
                        "files": {"main.py": "scripts/main.py"},
                        "config": {"feeds": "{{form.feeds}}"},
                    },
                },
            }
        )
    )
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "main.py").write_text("print('hello from bundle')\n")
    return repo_root


def _patch_catalog_preflight(monkeypatch):
    """Force the catalog fallback path and fake the repo fetch."""
    from openhands.automation.plugin_specs import PluginSourceError

    def _fake_fetch(*, source, cache_dir, ref, repo_path, **kw):
        return Path(source), "abc123"

    monkeypatch.setattr(
        "openhands.automation.plugin_specs.fetch_plugin_with_resolution",
        _fake_fetch,
    )

    class _InvalidPluginCM:
        def __enter__(self):
            raise PluginSourceError(
                "invalid_plugin", "Invalid plugin: plugin.json not found."
            )

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "openhands.automation.plugin_specs.preflight_plugin",
        lambda *a, **kw: _InvalidPluginCM(),
    )


def test_preflight_preserves_bundle_files(tmp_path):
    """Catalog manifest bundle.files must be preserved on the entrypoint."""
    repo_root = _write_catalog_manifest_with_bundle(tmp_path)

    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(repo_root)),
        allow_local_sources=True,
    )

    assert manifest.entrypoint.runner == "script"
    assert manifest.entrypoint.bundle_files == {"main.py": "scripts/main.py"}


def test_preflight_rejects_missing_bundle_file_source(tmp_path):
    """A bundle file source that doesn't exist in the repo must fail preflight."""
    catalog_dir = _write_catalog_manifest(tmp_path)
    # Intentionally do NOT create scripts/main.py — the bundle.files mapping
    # references it, so preflight must reject it before create/dispatch.

    with pytest.raises(PluginSpecError, match="Bundle file not found in repo"):
        preflight_manifest(
            AutomationPluginSource(source=str(catalog_dir)),
            allow_local_sources=True,
        )


def test_entrypoint_for_bundle_files_omits_plugin_root(tmp_path):
    """When bundle_files are present, the entrypoint runs from the tarball root."""
    from openhands.automation.plugin_specs import entrypoint_for_manifest

    repo_root = _write_catalog_manifest_with_bundle(tmp_path)
    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(repo_root)),
        allow_local_sources=True,
    )

    assert entrypoint_for_manifest(manifest) == "python3 main.py"


async def test_build_execution_payload_bundles_files_and_skips_fetch(
    tmp_path, monkeypatch
):
    """build_execution_payload must bundle catalog script files and skip
    the agent-server pre-fetch for the main plugin source."""
    repo_root = _write_catalog_manifest_with_bundle(tmp_path)
    _patch_catalog_preflight(monkeypatch)

    tarball, _env, runtime, plugin_fetches = await build_execution_payload(
        plugin_spec={
            "plugin": {"source": str(repo_root)},
            "config": {"feeds": "https://example.com/rss"},
        },
        agent_profile_id=None,
        agent_profile_name=None,
        llm_profile_name=None,
        work_dir="/tmp/automation-run",
    )

    # No main plugin source in fetch list — it's bundled in the tarball.
    assert plugin_fetches == []
    # Entrypoint is the raw command, no AUTOMATION_PLUGIN_ROOT prefix.
    assert runtime["entrypoint"] == "python3 main.py"

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "main.py" in names
        main_file = tar.extractfile("main.py")
        assert main_file is not None
        assert main_file.read() == b"print('hello from bundle')\n"
        assert ".automation/config.json" in names
        assert ".automation/execution.json" in names


async def test_build_execution_payload_renders_bundle_config_at_root(
    tmp_path, monkeypatch
):
    """Catalog bundle.config templates are rendered and written as config.json
    at the tarball root, with form arg names mapped to config file keys."""
    repo_root = _write_catalog_manifest_with_bundle(tmp_path)
    _patch_catalog_preflight(monkeypatch)

    tarball, _env, _runtime, _fetches = await build_execution_payload(
        plugin_spec={
            "plugin": {"source": str(repo_root)},
            "config": {"feeds": "https://example.com/rss\nhttps://example.org"},
        },
        agent_profile_id=None,
        agent_profile_name=None,
        llm_profile_name=None,
        work_dir="/tmp/automation-run",
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        config_file = tar.extractfile("config.json")
        assert config_file is not None
        rendered = json.loads(config_file.read().decode())
        assert rendered == {"feeds": "https://example.com/rss\nhttps://example.org"}


async def test_build_execution_payload_bundle_config_missing_form_value_raises(
    tmp_path, monkeypatch
):
    """A bundle.config template referencing an unsubmitted form arg (with no
    default) must raise PluginSpecError, not silently produce an empty config."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "test-catalog",
                "name": "Test Catalog Automation",
                "setup": {
                    "version": "1.0",
                    "mode": "direct",
                    "form": {
                        "triggers": {"cron": {"schedule": {"default": "0 8 * * *"}}},
                        "args": {"feeds": {"type": "textarea", "required": False}},
                    },
                    "bundle": {
                        "entrypoint": "python3 main.py",
                        "files": {"main.py": "scripts/main.py"},
                        "config": {"feeds": "{{form.feeds}}"},
                    },
                },
            }
        )
    )
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "main.py").write_text("print('hello')\n")
    _patch_catalog_preflight(monkeypatch)

    with pytest.raises(PluginSpecError, match="missing config value"):
        await build_execution_payload(
            plugin_spec={"plugin": {"source": str(repo_root)}, "config": {}},
            agent_profile_id=None,
            agent_profile_name=None,
            llm_profile_name=None,
            work_dir="/tmp/automation-run",
        )


def test_catalog_arg_multiple_true_produces_array_schema(tmp_path):
    """A catalog arg with multiple: true must produce an array schema, not a
    string schema (which would 422 on list values)."""
    catalog_dir = tmp_path / "catalog-plugin"
    catalog_dir.mkdir()
    (catalog_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "test-catalog",
                "name": "Test Catalog Automation",
                "setup": {
                    "version": "1.0",
                    "mode": "direct",
                    "form": {
                        "triggers": {"cron": {"schedule": {"default": "0 8 * * *"}}},
                        "args": {
                            "repositories": {
                                "type": "repo-picker",
                                "label": "Repos",
                                "multiple": True,
                                "required": True,
                            }
                        },
                    },
                    "bundle": {"entrypoint": "python3 main.py"},
                },
            }
        )
    )

    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(catalog_dir)),
        allow_local_sources=True,
    )

    prop = manifest.config_schema["properties"]["repositories"]
    assert prop["type"] == "array"
    assert prop["items"]["type"] == "string"


def test_catalog_arg_unknown_type_raises(tmp_path):
    """An unknown catalog field type must raise, not silently fall back to
    string."""
    catalog_dir = tmp_path / "catalog-plugin"
    catalog_dir.mkdir()
    (catalog_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "test-catalog",
                "name": "Test Catalog Automation",
                "setup": {
                    "version": "1.0",
                    "mode": "direct",
                    "form": {
                        "triggers": {"cron": {"schedule": {"default": "0 8 * * *"}}},
                        "args": {
                            "weird": {"type": "nonexistent-type", "required": True}
                        },
                    },
                    "bundle": {"entrypoint": "python3 main.py"},
                },
            }
        )
    )

    with pytest.raises(PluginSpecError, match="Unknown catalog field type"):
        preflight_manifest(
            AutomationPluginSource(source=str(catalog_dir)),
            allow_local_sources=True,
        )


def test_catalog_arg_pattern_constraint_is_translated(tmp_path):
    """A pattern constraint must be translated to the JSON Schema, not
    dropped."""
    catalog_dir = tmp_path / "catalog-plugin"
    catalog_dir.mkdir()
    (catalog_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "test-catalog",
                "name": "Test Catalog Automation",
                "setup": {
                    "version": "1.0",
                    "mode": "direct",
                    "form": {
                        "triggers": {"cron": {"schedule": {"default": "0 8 * * *"}}},
                        "args": {
                            "slug": {
                                "type": "string",
                                "constraints": {"pattern": "^[a-z-]+$"},
                            }
                        },
                    },
                    "bundle": {"entrypoint": "python3 main.py"},
                },
            }
        )
    )

    _resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(catalog_dir)),
        allow_local_sources=True,
    )

    assert manifest.config_schema["properties"]["slug"]["pattern"] == "^[a-z-]+$"


async def test_bundle_files_rejects_reserved_destination(tmp_path, monkeypatch):
    """A bundle file destination that collides with a reserved name must
    raise, not silently overwrite validated config."""
    repo_root = _write_catalog_manifest_with_bundle(tmp_path)
    _patch_catalog_preflight(monkeypatch)

    # Overwrite manifest to use a reserved destination
    manifest_data = json.loads((repo_root / "manifest.json").read_text())
    manifest_data["setup"]["bundle"]["files"] = {"config.json": "scripts/main.py"}
    (repo_root / "manifest.json").write_text(json.dumps(manifest_data))

    with pytest.raises(PluginSpecError, match="reserved"):
        await build_execution_payload(
            plugin_spec={"plugin": {"source": str(repo_root)}, "config": {}},
            agent_profile_id=None,
            agent_profile_name=None,
            llm_profile_name=None,
            work_dir="/tmp/automation-run",
        )


def test_bundle_files_rejects_working_dir_combo():
    """bundle_files cannot be combined with working_dir on a general
    (non-catalog) entrypoint."""
    from openhands.automation.plugin_specs import AutomationPluginEntrypoint

    with pytest.raises(ValueError, match="bundle_files.*working_dir"):
        AutomationPluginEntrypoint(
            runner="script",
            command="python3 main.py",
            working_dir="subdir",
            bundle_files={"main.py": "scripts/main.py"},
        )


def test_bundle_files_rejects_setup_script_combo():
    """bundle_files cannot be combined with setup_script."""
    from openhands.automation.plugin_specs import AutomationPluginEntrypoint

    with pytest.raises(ValueError, match="bundle_files.*setup_script"):
        AutomationPluginEntrypoint(
            runner="script",
            command="python3 main.py",
            setup_script="install.sh",
            bundle_files={"main.py": "scripts/main.py"},
        )


def test_preflight_returns_null_trigger_for_templated_manifest(tmp_path):
    """Preflight must return null suggested_trigger when the manifest uses a
    template (which needs config the preflight doesn't have), not 422."""
    plugin = write_plugin(tmp_path)
    data = json.loads((plugin / "automation-plugin.json").read_text())
    data["suggested_trigger_template"] = {
        "type": "cron",
        "schedule": "{{ json config.schedule }}",
        "timezone": "UTC",
    }
    data.pop("suggested_trigger", None)
    _write_automation_manifest(plugin, data)

    resolved_ref, manifest = preflight_manifest(
        AutomationPluginSource(source=str(plugin)),
        allow_local_sources=True,
    )
    assert manifest.suggested_trigger_template is not None
    assert manifest.suggested_trigger is None


async def test_build_execution_payload_bundle_files_missing_source(
    tmp_path, monkeypatch
):
    """A missing bundle file source must raise PluginSpecError."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "test-catalog",
                "name": "Test Catalog Automation",
                "setup": {
                    "version": "1.0",
                    "mode": "direct",
                    "form": {
                        "triggers": {
                            "cron": {
                                "schedule": {
                                    "default": "0 8 * * *",
                                    "required": True,
                                },
                                "timezone": {"default": "UTC", "required": True},
                            }
                        },
                        "args": {},
                    },
                    "bundle": {
                        "entrypoint": "python3 main.py",
                        "files": {"main.py": "scripts/missing.py"},
                    },
                },
            }
        )
    )
    _patch_catalog_preflight(monkeypatch)

    with pytest.raises(PluginSpecError, match="Bundle file not found"):
        await build_execution_payload(
            plugin_spec={"plugin": {"source": str(repo_root)}, "config": {}},
            agent_profile_id=None,
            agent_profile_name=None,
            llm_profile_name=None,
            work_dir="/tmp/automation-run",
        )


async def test_run_fetches_the_pinned_sha_stored_for_an_sa_owned_automation(
    tmp_path, monkeypatch
):
    """A stored full SHA is what each run asks the plugin source for.

    This holds for any stored ref (run-time preflight uses ``plugin.ref``), so
    pinning an SA-owned automation only needs the SHA stored at create.
    """
    from contextlib import contextmanager

    sha = "0123456789abcdef0123456789abcdef01234567"
    plugin = write_plugin(tmp_path)
    real_preflight = plugin_specs.preflight_plugin
    requested_refs: list[str | None] = []

    @contextmanager
    def preflight(**kwargs):
        requested_refs.append(kwargs["ref"])
        with real_preflight(**kwargs) as checked:
            yield checked

    monkeypatch.setattr(plugin_specs, "preflight_plugin", preflight)

    await build_execution_payload(
        plugin_spec={"plugin": {"source": str(plugin), "ref": sha}, "config": {}},
        agent_profile_id=None,
        agent_profile_name=None,
        llm_profile_name=None,
        work_dir="/workspace/project",
    )

    assert requested_refs == [sha]


async def test_run_fetches_the_pinned_secondary_plugins_not_the_moved_branch(
    tmp_path, monkeypatch
):
    """Stored ``required_plugins`` win over the manifest's mutable secondary refs."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    pinned = "0123456789abcdef0123456789abcdef01234567"
    moved = "ffffffffffffffffffffffffffffffffffffffff"
    plugin = write_plugin(tmp_path)
    secondary = write_plugin(tmp_path / "secondary")
    manifest_data = json.loads((plugin / "automation-plugin.json").read_text())
    manifest_data.setdefault("runtime", {})["plugins"] = [
        {"source": str(secondary), "ref": "main"}
    ]
    _write_automation_manifest(plugin, manifest_data)
    real_preflight = plugin_specs.preflight_plugin

    @contextmanager
    def preflight(**kwargs):
        if kwargs["source"] == str(secondary):
            # The branch has moved: a floating ref now resolves to `moved`.
            ref = kwargs["ref"]
            resolved = ref if ref and len(ref) == 40 else moved
            with real_preflight(**{**kwargs, "ref": None}):
                yield SimpleNamespace(resolved_ref=resolved)
        else:
            with real_preflight(**kwargs) as checked:
                yield checked

    monkeypatch.setattr(plugin_specs, "preflight_plugin", preflight)

    *_, plugin_fetches = await build_execution_payload(
        plugin_spec={
            "plugin": {"source": str(plugin)},
            "config": {},
            "required_plugins": [{"source": str(secondary), "ref": pinned}],
        },
        agent_profile_id=None,
        agent_profile_name=None,
        llm_profile_name=None,
        work_dir="/workspace/project",
    )

    assert plugin_fetches[1:] == [{"source": str(secondary), "ref": pinned}]


def _write_templated_arguments(plugin: Path, arguments: str) -> None:
    metadata = json.loads((plugin / ".plugin" / "plugin.json").read_text())
    metadata["automation"]["entrypoint"]["arguments"] = arguments
    metadata["automation"]["config_schema"] = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
    }
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(metadata))
    (plugin / "automation-plugin.json").write_text(
        json.dumps({"name": metadata["name"], **metadata["automation"]})
    )


async def test_conversation_runner_renders_arguments_from_config(tmp_path):
    plugin = write_conversation_plugin(tmp_path)
    _write_templated_arguments(plugin, "{{ config.prompt }}")

    tarball, _env, _runtime, _fetches = await build_execution_payload(
        plugin_spec={
            "plugin": {"source": str(plugin)},
            "config": {"prompt": "Summarize {{ open }} issues\nby label"},
        },
        agent_profile_id="profile-id",
        agent_profile_name="agent-default",
        llm_profile_name=None,
        work_dir="/tmp/automation-run",
    )

    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        prompt_file = tar.extractfile("prompt.txt")
        assert prompt_file is not None
        prompt = prompt_file.read().decode()
    assert prompt == (
        "/hello-world-automation:hello Summarize {{ open }} issues\nby label"
    )


def test_conversation_runner_arguments_need_their_config(tmp_path):
    plugin = write_conversation_plugin(tmp_path)
    _write_templated_arguments(plugin, "{{ config.prompt }}")
    _, _, manifest = fetch_manifest(AutomationPluginSource(source=str(plugin)))

    with pytest.raises(PluginSpecError, match="missing config value: config.prompt"):
        validate_plugin_config(manifest, {})

import importlib
import io
import json
import tarfile
import uuid
from pathlib import Path

from openhands.automation.dispatcher import outcome_run_env
from openhands.automation.plugin_specs import _load_plugin_spec_preset_files
from openhands.automation.preset_router import (
    _generate_plugin_tarball,
    _generate_tarball,
)
from openhands.automation.presets.outcome_prompt import (
    outcome_context_section,
    outcome_secret_env,
)


def test_outcome_env_is_factory_gated_and_uses_existing_run_token():
    factory_id = uuid.uuid4()
    parser = {"key": "score", "expression": "result.score", "value_type": "number"}
    assert outcome_run_env(None, [parser], "https://api.example", "run-token") == {}
    assert outcome_run_env(factory_id, [parser], "https://api.example", None) == {}
    assert outcome_run_env(factory_id, None, "https://api.example", "run-token") == {
        "AUTOMATION_OUTCOME_URL": "https://api.example/v1/outcomes"
    }
    env = outcome_run_env(factory_id, [parser], "https://api.example/", "run-token")
    schema = json.loads(env["AUTOMATION_OUTCOME_SCHEMA"])
    assert schema["properties"]["payload"]["x-openhands-outcome-values"][
        "required"
    ] == ["score"]


def test_prompt_guidance_is_credential_safe_and_describes_both_workflows(monkeypatch):
    preset_dir = Path(__file__).resolve().parents[1] / "openhands/automation/presets"
    monkeypatch.syspath_prepend(str(preset_dir))
    build_user_prompt = importlib.import_module(
        "openhands.automation.presets.plugin_spec.prompt_builder"
    ).build_user_prompt
    env = {
        "AUTOMATION_OUTCOME_URL": "https://api.example/v1/outcomes",
        "AUTOMATION_KV_TOKEN": "very-secret",
        "AUTOMATION_ID": "automation-1",
    }
    assert outcome_context_section({}) is None
    assert (
        outcome_context_section(
            {"AUTOMATION_OUTCOME_URL": env["AUTOMATION_OUTCOME_URL"]}
        )
        is None
    )
    assert outcome_secret_env({}) == {}
    assert outcome_secret_env(env) == {
        "AUTOMATION_OUTCOME_URL": env["AUTOMATION_OUTCOME_URL"],
        "AUTOMATION_KV_TOKEN": env["AUTOMATION_KV_TOKEN"],
    }
    section = outcome_context_section(env)
    assert section is not None
    assert "very-secret" not in section
    assert '"values"' in section
    assert '"payload"' in section
    assert "flat JSON object" in section
    assert "$AUTOMATION_KV_TOKEN" in section
    env["AUTOMATION_OUTCOME_SCHEMA"] = '{"required":["payload"]}'
    section = outcome_context_section(env)
    assert section is not None
    assert '"payload"' in section
    assert '"required"' in section
    assert (
        outcome_secret_env(env)["AUTOMATION_OUTCOME_SCHEMA"]
        == env["AUTOMATION_OUTCOME_SCHEMA"]
    )
    assert "## Outcome Reporting API" in build_user_prompt("Do the work", None, env)
    assert "very-secret" not in build_user_prompt("Do the work", None, env)
    assert build_user_prompt("Do the work", None, {}) == "Do the work"


def test_each_conversation_preset_bundles_shared_prompt_helper():
    for tarball in (
        _generate_tarball("Do it"),
        _generate_plugin_tarball([], "Do it"),
    ):
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            assert "outcome_prompt.py" in tar.getnames()
    assert "outcome_prompt.py" in _load_plugin_spec_preset_files()

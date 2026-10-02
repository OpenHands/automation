import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from openhands.automation import observability
from openhands.automation.models import AutomationRunStatus


def test_span_noop_does_not_swallow_body_exceptions(monkeypatch):
    monkeypatch.setattr(observability, "observability_enabled", lambda: False)

    with pytest.raises(RuntimeError, match="boom"):
        with observability.span("test.span"):
            raise RuntimeError("boom")


def test_inject_trace_context_sets_laminar_span_context(monkeypatch):
    from lmnr import Laminar

    monkeypatch.setattr(observability, "observability_enabled", lambda: True)
    monkeypatch.setattr(
        Laminar, "serialize_span_context", classmethod(lambda cls: "serialized-context")
    )
    carrier: dict[str, str] = {}

    observability.inject_trace_context(carrier)

    assert carrier["LMNR_SPAN_CONTEXT"] == "serialized-context"


def test_automation_env_metadata_includes_authoritative_ids():
    automation_id = uuid4()
    org_id = uuid4()
    user_id = uuid4()
    run_id = uuid4()
    automation = SimpleNamespace(
        id=automation_id,
        name="Trace me",
        org_id=org_id,
        user_id=user_id,
        trigger={"type": "event"},
    )
    run = SimpleNamespace(id=run_id, trigger_source="event")

    metadata = observability.automation_env_metadata(automation, run)  # type: ignore[arg-type]

    assert metadata["AUTOMATION_ID"] == str(automation_id)
    assert metadata["AUTOMATION_NAME"] == "Trace me"
    assert metadata["AUTOMATION_RUN_ID"] == str(run_id)
    assert metadata["AUTOMATION_ORG_ID"] == str(org_id)
    assert metadata["AUTOMATION_USER_ID"] == str(user_id)
    assert metadata["AUTOMATION_TRIGGER_SOURCE"] == "event"
    assert metadata["AUTOMATION_TRIGGER_TYPE"] == "event"
    assert metadata["AUTOMATION_RUN_TRIGGER_SOURCE"] == "event"
    assert metadata["OPENHANDS_OBSERVABILITY_SPAN_NAME"] == "automation.conversation"
    assert metadata["OPENHANDS_OBSERVABILITY_TAGS"] == (
        "automation,automation.trigger:event,automation.run_trigger:event"
    )
    observability_metadata = json.loads(metadata["OPENHANDS_OBSERVABILITY_METADATA"])
    assert observability_metadata["automation.id"] == str(automation_id)
    assert observability_metadata["automation.trigger_source"] == "event"
    assert observability_metadata["automation.run.trigger_source"] == "event"


def test_automation_attributes_filters_none_and_formats_run_status():
    automation_id = uuid4()
    run_id = uuid4()
    run = SimpleNamespace(
        id=run_id,
        automation_id=automation_id,
        status=AutomationRunStatus.RUNNING,
        trigger_source="manual",
        conversation_id=None,
        sandbox_id="sandbox-1",
        bash_command_id=None,
    )

    attrs = observability.automation_attributes(run=run)  # type: ignore[arg-type]

    assert attrs["automation.id"] == str(automation_id)
    assert attrs["automation.run_id"] == str(run_id)
    assert attrs["automation.run.status"] == "RUNNING"
    assert attrs["automation.sandbox_id"] == "sandbox-1"
    assert "automation.bash_command_id" not in attrs

import contextlib
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


def test_current_span_context_serializes_laminar_context(monkeypatch):
    from lmnr import Laminar

    monkeypatch.setattr(observability, "observability_enabled", lambda: True)
    monkeypatch.setattr(
        Laminar, "serialize_span_context", classmethod(lambda cls: "serialized-context")
    )

    assert observability.current_span_context() == "serialized-context"


def test_inject_trace_context_sets_laminar_span_context(monkeypatch):
    from lmnr import Laminar

    monkeypatch.setattr(observability, "observability_enabled", lambda: True)
    monkeypatch.setattr(
        Laminar, "serialize_span_context", classmethod(lambda cls: "serialized-context")
    )
    carrier: dict[str, str] = {}

    observability.inject_trace_context(carrier)

    assert carrier["LMNR_SPAN_CONTEXT"] == "serialized-context"


def test_span_uses_laminar_parent_span_context(monkeypatch):
    from lmnr import Laminar

    calls: dict[str, object] = {}
    attributes: dict[str, object] = {}

    class DummySpan:
        def set_attribute(self, key, value):
            attributes[key] = value

    @contextlib.contextmanager
    def fake_start_as_current_span(**kwargs):
        calls["start_kwargs"] = kwargs
        yield DummySpan()

    monkeypatch.setattr(observability, "observability_enabled", lambda: True)
    monkeypatch.setattr(
        Laminar,
        "deserialize_span_context",
        classmethod(lambda cls, value: {"deserialized": value}),
    )
    monkeypatch.setattr(
        Laminar,
        "start_as_current_span",
        classmethod(lambda cls, **kwargs: fake_start_as_current_span(**kwargs)),
    )

    with observability.span(
        "automation.callback.received",
        {"automation.run_id": "run-1"},
        parent_span_context="parent-context",
    ):
        pass

    assert calls["start_kwargs"] == {
        "name": "automation.callback.received",
        "parent_span_context": {"deserialized": "parent-context"},
    }
    assert attributes == {"automation.run_id": "run-1"}


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
    run = SimpleNamespace(
        id=run_id,
        trigger_source="event",
        observability_associations={
            "scm.repository.full_name": "OpenHands/automation",
            "scm.pull_request.number": 544,
        },
    )

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
    assert observability_metadata["scm.repository.full_name"] == "OpenHands/automation"
    assert observability_metadata["scm.pull_request.number"] == 544


def test_automation_attributes_filters_none_and_formats_run_status():
    automation_id = uuid4()
    run_id = uuid4()
    trigger_event_id = uuid4()
    run = SimpleNamespace(
        id=run_id,
        automation_id=automation_id,
        status=AutomationRunStatus.RUNNING,
        trigger_source="manual",
        trigger_event_id=trigger_event_id,
        conversation_id=None,
        sandbox_id="sandbox-1",
        bash_command_id=None,
    )

    attrs = observability.automation_attributes(run=run)  # type: ignore[arg-type]

    assert attrs["automation.id"] == str(automation_id)
    assert attrs["automation.run_id"] == str(run_id)
    assert attrs["automation.run.status"] == "RUNNING"
    assert attrs["automation.trigger_event_id"] == str(trigger_event_id)
    assert attrs["automation.sandbox_id"] == "sandbox-1"
    assert "automation.bash_command_id" not in attrs

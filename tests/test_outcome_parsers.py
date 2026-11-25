import pytest
from pydantic import ValidationError

from openhands.automation.outcome_parsers import (
    OutcomeParserDefinition,
    build_outcome_payload_schema,
    parse_outcome_payload,
    validate_outcome_parsers,
)
from openhands.automation.outcome_values import normalize_outcome_values
from openhands.automation.plugin_specs import CreatePluginSpecAutomationRequest
from openhands.automation.preset_router import (
    CreatePluginAutomationRequest,
    CreatePromptAutomationRequest,
)


PARSERS = [
    {"key": "score", "expression": "result.score", "value_type": "number"},
    {"key": "passed", "expression": "result.passed", "value_type": "boolean"},
]


def test_nested_payload_produces_values_for_shared_core_validator():
    values = parse_outcome_payload({"result": {"score": 4.5, "passed": True}}, PARSERS)
    rows = normalize_outcome_values(values)
    assert rows[0]["numeric_value"] == 4.5
    assert rows[1]["boolean_value"] is True


def test_missing_result_fails_whole_submission():
    with pytest.raises(ValueError, match="returned null or missing value"):
        parse_outcome_payload({"result": {"passed": True}}, PARSERS)


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"score": 4.5}},
        {"result": {"score": 4.5, "passed": None}},
    ],
)
def test_optional_parser_omits_missing_or_null_value(payload):
    parsers = [PARSERS[0], {**PARSERS[1], "required": False}]
    values = parse_outcome_payload(payload, parsers)
    assert [row["key"] for row in normalize_outcome_values(values)] == ["score"]
    assert (
        OutcomeParserDefinition.model_validate(parsers[1]).model_dump()["required"]
        is False
    )


def test_optional_parser_still_validates_present_values():
    parsers = [PARSERS[0], {**PARSERS[1], "required": False}]
    with pytest.raises(ValueError, match="is not a boolean"):
        normalize_outcome_values(
            parse_outcome_payload(
                {"result": {"score": 4.5, "passed": "invalid"}}, parsers
            )
        )


def test_parser_output_still_obeys_shared_numeric_guard():
    with pytest.raises(ValueError, match="2\\^53"):
        normalize_outcome_values(
            parse_outcome_payload(
                {"result": {"score": 2**53 + 1, "passed": True}}, PARSERS
            )
        )


def test_parser_schema_describes_extracted_keys():
    schema = build_outcome_payload_schema(PARSERS)
    assert schema is not None
    assert schema["required"] == ["payload"]
    values = schema["properties"]["payload"]["x-openhands-outcome-values"]
    assert values["required"] == ["score", "passed"]
    assert values["properties"]["score"]["type"] == "number"
    assert build_outcome_payload_schema(None) is None


def test_parser_schema_only_requires_non_optional_keys():
    parsers = [PARSERS[0], {**PARSERS[1], "required": False}]
    schema = build_outcome_payload_schema(parsers)
    assert schema is not None
    values = schema["properties"]["payload"]["x-openhands-outcome-values"]
    assert values["required"] == ["score"]
    assert set(values["properties"]) == {"score", "passed"}


def test_duplicate_parser_keys_rejected():
    parser = OutcomeParserDefinition.model_validate(PARSERS[0])
    with pytest.raises(ValueError, match="unique"):
        validate_outcome_parsers([parser, parser])


@pytest.mark.parametrize(
    ("request_type", "body"),
    [
        (
            CreatePromptAutomationRequest,
            {
                "name": "Prompt",
                "prompt": "Run",
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            },
        ),
        (
            CreatePluginAutomationRequest,
            {
                "name": "Plugin",
                "prompt": "Run",
                "plugins": [{"source": "github:example/plugin"}],
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            },
        ),
    ],
)
def test_each_create_request_validates_parser_keys(request_type, body):
    parser = PARSERS[0]
    request = request_type.model_validate({**body, "outcome_parsers": [parser]})
    assert request.outcome_parsers[0].key == "score"
    assert request.outcome_parsers[0].required is True
    assert request.outcome_parsers[0].model_dump() == parser
    optional_request = request_type.model_validate(
        {**body, "outcome_parsers": [{**parser, "required": False}]}
    )
    assert optional_request.outcome_parsers[0].model_dump()["required"] is False
    with pytest.raises(ValidationError, match="unique"):
        request_type.model_validate({**body, "outcome_parsers": [parser, parser]})


def test_plugin_spec_request_takes_no_outcome_parsers():
    body = {"name": "Plugin spec", "plugin": {"source": "github:example/plugin"}}
    CreatePluginSpecAutomationRequest.model_validate(body)
    with pytest.raises(ValidationError, match="outcome_parsers"):
        CreatePluginSpecAutomationRequest.model_validate(
            {**body, "outcome_parsers": [PARSERS[0]]}
        )

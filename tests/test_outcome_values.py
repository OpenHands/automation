import pytest

from openhands.automation.outcome_values import (
    build_outcome_payload_schema,
    extract_outcome_value_columns,
)


def test_flat_outcome_payload_accepts_scalar_values():
    columns = extract_outcome_value_columns(
        {
            "comments_posted": 4,
            "reviewed": True,
            "repository": "OpenHands/automation",
        },
        None,
    )

    by_key = {column["key"]: column for column in columns}
    assert by_key["comments_posted"]["numeric_value"] == 4
    assert by_key["reviewed"]["boolean_value"] is True
    assert by_key["repository"]["text_value"] == "OpenHands/automation"


def test_flat_outcome_payload_rejects_invalid_keys():
    with pytest.raises(ValueError):
        extract_outcome_value_columns({"bad key": 1}, None)


def test_flat_outcome_payload_rejects_nested_values():
    with pytest.raises(ValueError, match="must be a string, finite number, or boolean"):
        extract_outcome_value_columns({"review": {"comments_posted": 4}}, None)


def test_configured_outcome_parsers_extract_nested_values():
    columns = extract_outcome_value_columns(
        {"outcome": {"comments_posted": 4, "reviewed": True}},
        [
            {
                "key": "review.comments_posted",
                "expression": "outcome.comments_posted",
                "value_type": "number",
            },
            {
                "key": "review.reviewed",
                "expression": "outcome.reviewed",
                "value_type": "boolean",
            },
        ],
    )

    by_key = {column["key"]: column for column in columns}
    assert by_key["review.comments_posted"]["numeric_value"] == 4
    assert by_key["review.reviewed"]["boolean_value"] is True


def test_configured_outcome_parser_failure_rejects_submission():
    with pytest.raises(ValueError, match="returned null or missing value"):
        extract_outcome_value_columns(
            {"outcome": {"reviewed": True}},
            [
                {
                    "key": "review.comments_posted",
                    "expression": "outcome.comments_posted",
                    "value_type": "number",
                }
            ],
        )


def test_configured_outcome_parsers_build_request_schema():
    schema = build_outcome_payload_schema(
        [
            {
                "key": "score",
                "expression": "result.score",
                "value_type": "number",
            },
            {
                "key": "finished_at",
                "expression": "finished_at",
                "value_type": "datetime",
            },
        ]
    )

    assert schema is not None
    assert schema["required"] == ["payload"]
    outcome_values = schema["properties"]["payload"]["x-openhands-outcome-values"]
    assert outcome_values["required"] == ["score", "finished_at"]
    assert outcome_values["properties"]["score"]["type"] == "number"
    assert outcome_values["properties"]["finished_at"]["format"] == "date-time"
    assert "result.score" in outcome_values["properties"]["score"]["description"]


def test_empty_outcome_parsers_do_not_build_request_schema():
    assert build_outcome_payload_schema(None) is None
    assert build_outcome_payload_schema([]) is None

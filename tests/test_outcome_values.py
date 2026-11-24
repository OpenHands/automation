import pytest

from openhands.automation.outcome_values import (
    extract_outcome_value_columns,
    normalize_outcome_values,
)


def test_flat_outcome_payload_accepts_scalar_values():
    columns = extract_outcome_value_columns(
        {
            "comments_posted": 4,
            "reviewed": True,
            "repository": "OpenHands/automation",
        },
    )

    by_key = {column["key"]: column for column in columns}
    assert by_key["comments_posted"]["numeric_value"] == 4
    assert by_key["reviewed"]["boolean_value"] is True
    assert by_key["repository"]["text_value"] == "OpenHands/automation"


def test_flat_outcome_payload_rejects_invalid_keys():
    with pytest.raises(ValueError):
        extract_outcome_value_columns({"bad key": 1})


def test_flat_outcome_payload_rejects_nested_values():
    with pytest.raises(ValueError, match="must be a string, finite number, or boolean"):
        extract_outcome_value_columns({"review": {"comments_posted": 4}})


def test_direct_typed_values_accept_json_datetime_and_boolean():
    columns = normalize_outcome_values(
        [
            {"key": "metadata", "value_type": "json", "value": {"issue": 1}},
            {
                "key": "finished_at",
                "value_type": "datetime",
                "value": "2026-11-23T12:00:00Z",
            },
            {"key": "passed", "value_type": "boolean", "value": False},
        ]
    )
    assert columns[0]["json_value"] == {"issue": 1}
    assert columns[1]["datetime_value"].year == 2026
    assert columns[2]["boolean_value"] is False


@pytest.mark.parametrize("number", [2**53 + 1, -(2**53 + 1), 10**1000])
def test_oversized_numbers_are_rejected_before_float_conversion(number):
    with pytest.raises(ValueError, match="2\\^53"):
        normalize_outcome_values(
            [{"key": "score", "value_type": "number", "value": number}]
        )


def test_direct_typed_values_reject_duplicate_keys():
    with pytest.raises(ValueError, match="unique"):
        normalize_outcome_values(
            [
                {"key": "score", "value_type": "number", "value": 1},
                {"key": "score", "value_type": "number", "value": 2},
            ]
        )

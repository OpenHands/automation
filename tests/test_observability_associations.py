import pytest

from openhands.automation.observability_associations import (
    evaluate_observability_associations,
    validate_observability_associations,
)


def test_validate_observability_associations_compiles_jmespath() -> None:
    assert validate_observability_associations(
        {
            "scm.repository.full_name": "repository.full_name",
            "automation.subject.kind": "'pull_request'",
        }
    ) == {
        "scm.repository.full_name": "repository.full_name",
        "automation.subject.kind": "'pull_request'",
    }


@pytest.mark.parametrize(
    "associations",
    [
        {"bad key": "repository.full_name"},
        {"scm.repository": "repository.["},
        {"scm.repository": ""},
        {"scm.repository": 123},
        ["not", "a", "mapping"],
    ],
)
def test_validate_observability_associations_rejects_invalid_config(
    associations,
) -> None:
    with pytest.raises(ValueError):
        validate_observability_associations(associations)


def test_evaluate_observability_associations_keeps_only_scalars() -> None:
    result = evaluate_observability_associations(
        {
            "scm.repository.full_name": "repository.full_name",
            "scm.pull_request.number": "pull_request.number",
            "automation.subject.id": (
                "join('', [repository.full_name, '#', to_string(pull_request.number)])"
            ),
            "ignored.object": "repository",
            "ignored.missing": "does_not_exist",
        },
        {
            "repository": {"full_name": "OpenHands/automation"},
            "pull_request": {"number": 544},
        },
    )

    assert result == {
        "scm.repository.full_name": "OpenHands/automation",
        "scm.pull_request.number": 544,
        "automation.subject.id": "OpenHands/automation#544",
    }

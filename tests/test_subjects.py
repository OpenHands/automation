"""Tests for the subject -> conversation id rule."""

import uuid

from openhands.automation.models import Automation, AutomationRunAsKind
from openhands.automation.subjects import conversation_id_for, service_account_id_of


ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
AUTOMATION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def test_user_owned_id_is_pinned():
    """Existing threads keep their ids: this value must never change."""
    assert (
        conversation_id_for(ORG_ID, AUTOMATION_ID, "slack", "T1/C1/1.1", None)
        == "5bd6e8d0-d61b-5ab8-8a76-32565e8d8110"
    )


def test_service_account_changes_the_id():
    user_owned = conversation_id_for(ORG_ID, AUTOMATION_ID, "slack", "T1/C1/1.1", None)
    service_owned = conversation_id_for(
        ORG_ID, AUTOMATION_ID, "slack", "T1/C1/1.1", OWNER_ID
    )
    other_service = conversation_id_for(
        ORG_ID, AUTOMATION_ID, "slack", "T1/C1/1.1", uuid.uuid4()
    )

    assert len({user_owned, service_owned, other_service}) == 3


def test_service_account_id_of_follows_run_as_kind():
    """A change to a service account re-keys; a change back restores."""
    automation = Automation(user_id=OWNER_ID, run_as_kind=AutomationRunAsKind.USER)
    assert service_account_id_of(automation) is None

    automation.run_as_kind = AutomationRunAsKind.SERVICE
    assert service_account_id_of(automation) == OWNER_ID

    automation.run_as_kind = AutomationRunAsKind.USER
    automation.user_id = uuid.uuid4()
    assert service_account_id_of(automation) is None

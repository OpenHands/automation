"""Which automations a user may see."""

from sqlalchemy import ColumnElement, and_, or_

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.models import Automation, AutomationVisibility


def visible_to(user: AuthenticatedUser) -> ColumnElement[bool]:
    """Filter for the automations of the user's org that the user may see.

    A private automation is seen by whoever may modify it (see
    ``_assert_can_manage``): holders of ``manage_automations``, and the person
    it runs as. Only an automation that runs as a person can be private, so
    ``user_id`` is never a service account here.
    """
    in_org = Automation.org_id == user.org_id
    if "manage_automations" in user.permissions:
        return in_org
    return and_(
        in_org,
        or_(
            Automation.visibility != AutomationVisibility.PRIVATE,
            Automation.user_id == user.user_id,
        ),
    )

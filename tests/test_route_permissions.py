"""Which API routes org members cannot use at all."""

import inspect

from fastapi.routing import APIRoute

from openhands.automation.app import app
from openhands.automation.auth import require_permission


# Members hold view_automations but not manage_automations, so they cannot call
# any of these routes. A member's own automations, drafts and uploads need only
# view_automations plus an ownership check in the handler. Draft routes and
# upload deletion once required manage_automations, so members could create
# automations but not save a draft or delete their own upload. Add a route here
# only when members must not use it at all.
MANAGE_ONLY_ROUTES = {
    ("POST", "/api/automation/v1/webhooks"),
    ("PATCH", "/api/automation/v1/webhooks/{webhook_id}"),
    ("DELETE", "/api/automation/v1/webhooks/{webhook_id}"),
    ("POST", "/api/automation/v1/webhooks/{webhook_id}/rotate-secret"),
    ("PUT", "/api/automation/v1/git-sync/config"),
    ("POST", "/api/automation/v1/git-sync/check"),
    ("POST", "/api/automation/v1/git-sync/sync"),
}

# Every check that require_permission() returns shares this code object.
_PERMISSION_CHECK_CODE = require_permission("view_automations").__code__


def _required_permissions(route: APIRoute) -> set[str]:
    permissions: set[str] = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        call = dependant.call
        code = getattr(call, "__code__", None)
        if call is not None and code is _PERMISSION_CHECK_CODE:
            permissions.add(inspect.getclosurevars(call).nonlocals["permission"])
        pending.extend(dependant.dependencies)
    return permissions


def test_only_listed_routes_require_manage_automations():
    manage_only = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        and "manage_automations" in _required_permissions(route)
        for method in route.methods
    }

    assert manage_only == MANAGE_ONLY_ROUTES

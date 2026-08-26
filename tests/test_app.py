from types import SimpleNamespace

import pytest

from openhands.automation.app import _workspace_purger_enabled


@pytest.mark.parametrize(
    ("is_local_mode", "retention_seconds", "expected"),
    [(True, 3600, True), (True, 0, False), (False, 3600, False)],
)
def test_workspace_purger_startup_gate(is_local_mode, retention_seconds, expected):
    settings = SimpleNamespace(
        is_local_mode=is_local_mode,
        workspace_retention_seconds=retention_seconds,
    )

    assert _workspace_purger_enabled(settings) is expected

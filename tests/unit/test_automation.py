"""Unit tests for the openhands_automation package."""

import openhands_automation


def test_version():
    """Verify the package exposes a version string."""
    assert isinstance(openhands_automation.__version__, str)
    assert openhands_automation.__version__ == "0.1.0"


def test_import():
    """Verify the openhands_automation package can be imported."""
    assert hasattr(openhands_automation, "__version__")

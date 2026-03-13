"""Unit tests for the automation package."""

import automation


def test_version():
    """Verify the package exposes a version string."""
    assert isinstance(automation.__version__, str)
    assert automation.__version__ == "0.1.0"


def test_import():
    """Verify the automation package can be imported."""
    assert hasattr(automation, "__version__")

import importlib.metadata

from openhands.automation.utils.version import SDK_PACKAGE_NAME, get_sdk_version


def test_get_sdk_version_caches_package_metadata_lookup(monkeypatch):
    calls = 0

    def package_version(name: str) -> str:
        nonlocal calls
        calls += 1
        assert name == SDK_PACKAGE_NAME
        return "1.2.3"

    monkeypatch.setattr(importlib.metadata, "version", package_version)
    get_sdk_version.cache_clear()
    try:
        assert get_sdk_version() == "1.2.3"
        assert get_sdk_version() == "1.2.3"
        assert calls == 1
    finally:
        get_sdk_version.cache_clear()

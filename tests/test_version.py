import importlib.metadata
from typing import cast

from openhands.automation.utils.version import _sdk_direct_url_install_spec


class FakeDistribution:
    version = "1.42.1"

    def __init__(self, direct_url_text: str | None):
        self._direct_url_text = direct_url_text

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return self._direct_url_text


def test_sdk_direct_url_install_spec_uses_git_commit_and_subdirectory():
    distribution = FakeDistribution(
        """
        {
          "url": "https://github.com/OpenHands/software-agent-sdk.git",
          "vcs_info": {
            "vcs": "git",
            "requested_revision": "main",
            "commit_id": "6c8380d72aaa3a943e6f8b972bd2488660274c5b"
          },
          "subdirectory": "openhands-sdk"
        }
        """
    )

    assert _sdk_direct_url_install_spec(
        cast(importlib.metadata.Distribution, distribution)
    ) == (
        "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git"
        "@6c8380d72aaa3a943e6f8b972bd2488660274c5b"
        "#subdirectory=openhands-sdk"
    )


def test_sdk_direct_url_install_spec_ignores_non_git_distribution():
    distribution = FakeDistribution('{"url": "https://files.pythonhosted.org/pkg.whl"}')

    assert (
        _sdk_direct_url_install_spec(
            cast(importlib.metadata.Distribution, distribution)
        )
        is None
    )


def test_tools_install_spec_uses_matching_git_subdirectory(monkeypatch):
    monkeypatch.setattr(
        "openhands.automation.utils.version.get_sdk_install_spec",
        lambda: (
            "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git"
            "@6c8380d72aaa3a943e6f8b972bd2488660274c5b"
            "#subdirectory=openhands-sdk"
        ),
    )

    from openhands.automation.utils.version import get_tools_install_spec

    assert get_tools_install_spec() == (
        "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git"
        "@6c8380d72aaa3a943e6f8b972bd2488660274c5b"
        "#subdirectory=openhands-tools"
    )


def test_tools_install_spec_falls_back_to_matching_sdk_version(monkeypatch):
    monkeypatch.setattr(
        "openhands.automation.utils.version.get_sdk_install_spec",
        lambda: "openhands-sdk==1.42.1",
    )
    monkeypatch.setattr(
        "openhands.automation.utils.version.get_sdk_version",
        lambda: "1.42.1",
    )

    from openhands.automation.utils.version import get_tools_install_spec

    assert get_tools_install_spec() == "openhands-tools==1.42.1"

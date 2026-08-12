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
            "commit_id": "96705f910ee6ab8649aa5733abb5c97d5e0a4822"
          },
          "subdirectory": "openhands-sdk"
        }
        """
    )

    assert _sdk_direct_url_install_spec(cast(importlib.metadata.Distribution, distribution)) == (
        "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git"
        "@96705f910ee6ab8649aa5733abb5c97d5e0a4822"
        "#subdirectory=openhands-sdk"
    )


def test_sdk_direct_url_install_spec_ignores_non_git_distribution():
    distribution = FakeDistribution('{"url": "https://files.pythonhosted.org/pkg.whl"}')

    assert _sdk_direct_url_install_spec(cast(importlib.metadata.Distribution, distribution)) is None

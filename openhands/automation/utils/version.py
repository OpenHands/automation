"""Version metadata helpers for the automation service."""

import importlib.metadata
from typing import TypedDict

from openhands.automation import __version__


SDK_PACKAGE_NAME = "openhands-sdk"
SDK_GIT_REF = "b3aacc186f84f5093707863fb1e8828a7202b57f"
SDK_GIT_REPOSITORY = "https://github.com/OpenHands/software-agent-sdk.git"


class ServerVersionInfo(TypedDict):
    package_version: str
    sdk_version: str
    sdk_git_ref: str


def get_sdk_version() -> str:
    return importlib.metadata.version(SDK_PACKAGE_NAME)


def get_server_version_info(
    *, missing_sdk_version: str | None = None
) -> ServerVersionInfo:
    try:
        sdk_version = get_sdk_version()
    except importlib.metadata.PackageNotFoundError:
        if missing_sdk_version is None:
            raise
        sdk_version = missing_sdk_version
    return {
        "package_version": __version__,
        "sdk_version": sdk_version,
        "sdk_git_ref": SDK_GIT_REF,
    }

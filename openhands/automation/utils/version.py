"""Version metadata helpers for the automation service."""

import importlib.metadata
from typing import TypedDict

from openhands.automation import __version__


SDK_PACKAGE_NAME = "openhands-sdk"
SDK_SOURCE_REPO = "https://github.com/OpenHands/software-agent-sdk.git"
SDK_SOURCE_REF = "73fabfd76491940fcb1a042289a18ad618ec89d7"
SDK_PACKAGE_SUBDIRECTORIES = (
    "openhands-sdk",
    "openhands-tools",
    "openhands-workspace",
)
SDK_INSTALL_SPECS = [
    f"{name} @ git+{SDK_SOURCE_REPO}@{SDK_SOURCE_REF}#subdirectory={name}"
    for name in SDK_PACKAGE_SUBDIRECTORIES
]


class ServerVersionInfo(TypedDict):
    package_version: str
    sdk_version: str


def get_sdk_version() -> str:
    return importlib.metadata.version(SDK_PACKAGE_NAME)


def get_sdk_install_specs() -> list[str]:
    return SDK_INSTALL_SPECS.copy()


def get_server_version_info(
    *, missing_sdk_version: str | None = None
) -> ServerVersionInfo:
    try:
        sdk_version = get_sdk_version()
    except importlib.metadata.PackageNotFoundError:
        if missing_sdk_version is None:
            raise
        sdk_version = missing_sdk_version
    return {"package_version": __version__, "sdk_version": sdk_version}

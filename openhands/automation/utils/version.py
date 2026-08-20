"""Version metadata helpers for the automation service."""

import importlib.metadata
from typing import TypedDict

from openhands.automation import __version__


SDK_PACKAGE_NAME = "openhands-sdk"
SDK_PACKAGE_NAMES = (
    "openhands-sdk",
    "openhands-tools",
    "openhands-workspace",
)


class ServerVersionInfo(TypedDict):
    package_version: str
    sdk_version: str


def get_sdk_version() -> str:
    return importlib.metadata.version(SDK_PACKAGE_NAME)


def get_sdk_install_specs(version: str | None = None) -> list[str]:
    sdk_version = version or get_sdk_version()
    return [f"{name}=={sdk_version}" for name in SDK_PACKAGE_NAMES]


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

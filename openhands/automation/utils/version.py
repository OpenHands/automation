"""Version metadata helpers for the automation service."""

import importlib.metadata
from typing import Final, TypedDict

from openhands.automation import __version__


SDK_PACKAGE_NAME = "openhands-sdk"
SDK_TOOLS_PACKAGE_NAME: Final[str] = "openhands-tools"
SDK_WORKSPACE_PACKAGE_NAME: Final[str] = "openhands-workspace"
SDK_REPOSITORY_URL: Final[str] = "https://github.com/OpenHands/software-agent-sdk.git"
# Temporary cross-repo pin for OpenHands/software-agent-sdk#5462 until released.
SDK_OBSERVABILITY_REF: Final[str] = "ab79e382bf5946e9ca8543cfc1e0a502508d68f1"
SDK_PACKAGE_SUBDIRECTORIES: Final[dict[str, str]] = {
    SDK_PACKAGE_NAME: "openhands-sdk",
    SDK_TOOLS_PACKAGE_NAME: "openhands-tools",
    SDK_WORKSPACE_PACKAGE_NAME: "openhands-workspace",
}


class SdkInstallInfo(TypedDict):
    version: str
    packages: dict[str, str]


class ServerVersionInfo(TypedDict):
    package_version: str
    sdk_version: str


def get_sdk_package_specs() -> dict[str, str]:
    return {
        package_name: (
            f"{package_name} @ git+{SDK_REPOSITORY_URL}"
            f"@{SDK_OBSERVABILITY_REF}#subdirectory={subdirectory}"
        )
        for package_name, subdirectory in SDK_PACKAGE_SUBDIRECTORIES.items()
    }


def get_sdk_install_info() -> SdkInstallInfo:
    return {"version": get_sdk_version(), "packages": get_sdk_package_specs()}


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
    return {"package_version": __version__, "sdk_version": sdk_version}

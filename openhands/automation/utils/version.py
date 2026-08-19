"""Version metadata helpers for the automation service."""

import importlib.metadata
import json
from typing import Any, TypedDict

from openhands.automation import __version__


SDK_PACKAGE_NAME = "openhands-sdk"
TOOLS_PACKAGE_NAME = "openhands-tools"


class ServerVersionInfo(TypedDict):
    package_version: str
    sdk_version: str


def get_sdk_version() -> str:
    return importlib.metadata.version(SDK_PACKAGE_NAME)


def _direct_url_install_spec(
    distribution: importlib.metadata.Distribution, package_name: str
) -> str | None:
    direct_url_text = distribution.read_text("direct_url.json")
    if not direct_url_text:
        return None
    try:
        direct_url: dict[str, Any] = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return None

    vcs_info = direct_url.get("vcs_info")
    url = direct_url.get("url")
    if not isinstance(vcs_info, dict) or vcs_info.get("vcs") != "git":
        return None
    if not isinstance(url, str) or not url:
        return None

    commit_id = vcs_info.get("commit_id") or vcs_info.get("requested_revision")
    if not isinstance(commit_id, str) or not commit_id:
        return None

    spec = f"{package_name} @ git+{url}@{commit_id}"
    subdirectory = direct_url.get("subdirectory")
    if isinstance(subdirectory, str) and subdirectory:
        spec = f"{spec}#subdirectory={subdirectory}"
    return spec


def _sdk_direct_url_install_spec(
    distribution: importlib.metadata.Distribution,
) -> str | None:
    return _direct_url_install_spec(distribution, SDK_PACKAGE_NAME)


def get_sdk_install_spec() -> str:
    distribution = importlib.metadata.distribution(SDK_PACKAGE_NAME)
    if direct_spec := _direct_url_install_spec(distribution, SDK_PACKAGE_NAME):
        return direct_spec
    return f"{SDK_PACKAGE_NAME}=={distribution.version}"


def get_tools_install_spec() -> str:
    sdk_install_spec = get_sdk_install_spec()
    if sdk_install_spec.startswith(f"{SDK_PACKAGE_NAME} @ git+"):
        return sdk_install_spec.replace(
            f"{SDK_PACKAGE_NAME} @", f"{TOOLS_PACKAGE_NAME} @", 1
        ).replace("#subdirectory=openhands-sdk", "#subdirectory=openhands-tools")
    return f"{TOOLS_PACKAGE_NAME}=={get_sdk_version()}"


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

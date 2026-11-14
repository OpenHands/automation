"""Hand an automation's repositories to the app server's conversation start.

Preset automations record their repositories as SDK ``RepoSource`` values: a
full URL, or ``owner/repo`` plus a provider. The app server's start request
names a repository the way its git providers do (``owner/repo``,
``group/subgroup/repo``, ``org/project/repo`` for Azure DevOps,
``PROJECT/repo`` for Bitbucket Data Center) and clones it from the host its
integration is configured for. It then runs the repository's setup script and
loads its skills and instruction files, as for an interactive conversation.

A repository that cannot be expressed that way (a commit SHA as its ref, an
SSH or file URL, a host this module cannot place) is left to the runner, which
clones it itself as before. So is one the owner has no git connection for: the
app server clones only through the owner's connection to the repository's
provider, on that connection's host, while the runner clones a public
repository anonymously.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit


# The app server clones at most this many repositories beside the primary.
MAX_ADDITIONAL_REPOSITORIES = 9

_PUBLIC_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
    "dev.azure.com": "azure_devops",
}
_PROVIDERS = frozenset(
    {"github", "gitlab", "bitbucket", "azure_devops", "bitbucket_data_center"}
)
# Where a connection that records no host clones from.
_HOSTED = {"github": "github.com", "gitlab": "gitlab.com", "bitbucket": "bitbucket.org"}
# ``git clone --branch`` takes branches and tags, not commits.
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


@dataclass(frozen=True)
class AppServerRepository:
    repository: str
    git_provider: str
    branch: str | None = None


@dataclass
class RepositorySplit:
    """The repositories the app server clones and those left to the runner."""

    app_server: list[AppServerRepository] = field(default_factory=list)
    runner: list[dict[str, Any]] = field(default_factory=list)


def _segments(path: str) -> list[str] | None:
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][: -len(".git")]
    if not parts or not all(_SEGMENT.match(p) and p not in (".", "..") for p in parts):
        return None
    return parts


def _provider_for_host(host: str) -> str | None:
    if host in _PUBLIC_HOSTS:
        return _PUBLIC_HOSTS[host]
    if host.endswith(".visualstudio.com"):
        return "azure_devops"
    return None


def _name_from_url(url: str, provider: str | None) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower()
    provider = provider or _provider_for_host(host)
    parts = _segments(parsed.path)
    if provider is None or parts is None:
        return None
    if provider == "azure_devops":
        if host.endswith(".visualstudio.com"):
            parts = [host.split(".", 1)[0], *parts]
        # org/project/_git/repo
        if len(parts) == 4 and parts[2] == "_git":
            return "/".join((parts[0], parts[1], parts[3])), provider
        return None
    if provider == "bitbucket_data_center":
        # https://host/scm/PROJECT/repo.git or https://host/projects/P/repos/r/...
        if len(parts) == 3 and parts[0] == "scm":
            return f"{parts[1]}/{parts[2]}", provider
        if len(parts) >= 4 and parts[0] == "projects" and parts[2] == "repos":
            return f"{parts[1]}/{parts[3]}", provider
        return None
    if provider == "gitlab":
        if "-" in parts:
            parts = parts[: parts.index("-")]
        return ("/".join(parts), provider) if len(parts) >= 2 else None
    # github, bitbucket: owner/repo, optionally followed by a web path.
    return ("/".join(parts[:2]), provider) if len(parts) >= 2 else None


def _hostname(value: str | None) -> str | None:
    """The host of a URL or of a bare host name, lowercased, without its port."""
    if not value:
        return None
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower() or None


def _connected(
    connections: Mapping[str, str | None], provider: str, host: str | None
) -> bool:
    """Whether the owner's connection to ``provider`` clones from ``host``.

    ``host`` is None for an ``owner/repo`` name, which means the connected
    host. An Azure DevOps connection records an organization, not a host, and
    its repositories always live on Azure DevOps.
    """
    if provider not in connections:
        return False
    if host is None or provider == "azure_devops":
        return True
    return (_hostname(connections[provider]) or _HOSTED.get(provider)) == host


def app_server_repository(
    repo: dict[str, Any], connections: Mapping[str, str | None] | None = None
) -> AppServerRepository | None:
    """The app server's name for a recorded repository, or None to leave it.

    With the owner's ``connections`` (``{provider: host}``), a repository on a
    provider or host the owner is not connected to is left to the runner too.
    """
    url = repo.get("url")
    provider = repo.get("provider")
    ref = repo.get("ref")
    if not isinstance(url, str) or (
        provider is not None and provider not in _PROVIDERS
    ):
        return None
    if ref is not None and (not isinstance(ref, str) or _COMMIT_SHA.match(ref)):
        return None
    if "://" in url:
        named = _name_from_url(url, provider)
    elif url.startswith("git@"):
        named = None
    else:
        parts = _segments(url)
        named = (
            ("/".join(parts), provider)
            if parts and len(parts) == 2 and provider
            else None
        )
    if named is None:
        return None
    name, resolved_provider = named
    host = _hostname(url) if "://" in url else None
    if connections is not None and not _connected(connections, resolved_provider, host):
        return None
    return AppServerRepository(
        repository=name, git_provider=resolved_provider, branch=ref
    )


def split_repositories(
    repos: list[dict[str, Any]] | None,
    connections: Mapping[str, str | None] | None = None,
) -> RepositorySplit:
    """Split an automation's repositories between the app server and the runner."""
    split = RepositorySplit()
    for repo in repos or []:
        mapped = (
            app_server_repository(repo, connections) if isinstance(repo, dict) else None
        )
        if mapped is None or len(split.app_server) > MAX_ADDITIONAL_REPOSITORIES:
            # The primary plus MAX_ADDITIONAL_REPOSITORIES are already taken.
            split.runner.append(repo)
        else:
            split.app_server.append(mapped)
    return split

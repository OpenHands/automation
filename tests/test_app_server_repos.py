"""Which of a preset's repositories the app server clones, and under what name."""

import pytest

from openhands.automation.utils.app_server_repos import (
    MAX_ADDITIONAL_REPOSITORIES,
    AppServerRepository,
    app_server_repository,
    split_repositories,
)


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        (
            {"url": "https://github.com/acme/api"},
            AppServerRepository("acme/api", "github"),
        ),
        (
            {"url": "https://github.com/acme/api.git", "ref": "release/2.0"},
            AppServerRepository("acme/api", "github", "release/2.0"),
        ),
        (
            {"url": "https://github.com/acme/api/tree/main/docs"},
            AppServerRepository("acme/api", "github"),
        ),
        (
            {"url": "acme/api", "provider": "github"},
            AppServerRepository("acme/api", "github"),
        ),
        (
            {"url": "https://gitlab.com/group/sub/api/-/tree/main"},
            AppServerRepository("group/sub/api", "gitlab"),
        ),
        (
            {"url": "https://bitbucket.org/team/api"},
            AppServerRepository("team/api", "bitbucket"),
        ),
        (
            {"url": "https://dev.azure.com/org/project/_git/api"},
            AppServerRepository("org/project/api", "azure_devops"),
        ),
        (
            {"url": "https://org.visualstudio.com/project/_git/api"},
            AppServerRepository("org/project/api", "azure_devops"),
        ),
        (
            {
                "url": "https://bitbucket.example.com/scm/PROJ/api.git",
                "provider": "bitbucket_data_center",
            },
            AppServerRepository("PROJ/api", "bitbucket_data_center"),
        ),
        (
            {
                "url": "https://bitbucket.example.com/projects/PROJ/repos/api/browse",
                "provider": "bitbucket_data_center",
            },
            AppServerRepository("PROJ/api", "bitbucket_data_center"),
        ),
    ],
)
def test_names_a_repository_the_way_the_app_server_does(repo, expected):
    assert app_server_repository(repo) == expected


@pytest.mark.parametrize(
    "repo",
    [
        # git clone --branch takes branches and tags, not commits.
        {"url": "https://github.com/acme/api", "ref": "0123abcd"},
        {"url": "https://github.com/acme/api", "ref": "0" * 40},
        # The app server clones over HTTPS from its configured host.
        {"url": "git@github.com:acme/api.git"},
        {"url": "http://github.com/acme/api"},
        {"url": "file:///srv/git/api"},
        # A host it cannot place without a provider: a Data Center repository
        # recorded by URL alone stays with the runner.
        {"url": "https://bitbucket.example.com/scm/PROJ/api.git"},
        # Credentials in the URL are never forwarded.
        {"url": "https://user:pass@github.com/acme/api"},
        {"url": "acme/api"},
        {"url": "acme/api", "provider": "sourcehut"},
        {"url": "https://github.com/acme"},
        {"url": "https://github.com/acme/../etc"},
        {"url": "https://dev.azure.com/org/project"},
        {"url": 42},
    ],
)
def test_leaves_what_it_cannot_name_to_the_runner(repo):
    assert app_server_repository(repo) is None


def test_split_keeps_order_and_sends_the_rest_to_the_runner():
    repos = [
        {"url": "https://github.com/acme/api"},
        {"url": "git@github.com:acme/ssh.git"},
        {"url": "https://gitlab.com/group/web", "ref": "main"},
        "not-a-dict",
    ]

    split = split_repositories(repos)

    assert split.app_server == [
        AppServerRepository("acme/api", "github"),
        AppServerRepository("group/web", "gitlab", "main"),
    ]
    assert split.runner == [{"url": "git@github.com:acme/ssh.git"}, "not-a-dict"]


def test_split_caps_what_the_app_server_takes():
    repos = [
        {"url": f"https://github.com/acme/r{i}"}
        for i in range(MAX_ADDITIONAL_REPOSITORIES + 3)
    ]

    split = split_repositories(repos)

    assert len(split.app_server) == MAX_ADDITIONAL_REPOSITORIES + 1
    assert split.runner == repos[MAX_ADDITIONAL_REPOSITORIES + 1 :]


def test_split_of_nothing():
    assert split_repositories(None).app_server == []
    assert split_repositories([]).runner == []


# The owner's git connections, as the settings' ``provider_tokens_set``.
@pytest.mark.parametrize(
    ("repo", "connections", "expected"),
    [
        # A public repository on a provider the owner never connected: the app
        # server cannot clone it; the runner clones it anonymously.
        (
            {"url": "https://github.com/acme/public"},
            {"gitlab": "gitlab.example.com"},
            None,
        ),
        ({"url": "https://github.com/acme/public"}, {}, None),
        # Connected provider and host.
        (
            {"url": "https://github.com/acme/api"},
            {"github": None},
            AppServerRepository("acme/api", "github"),
        ),
        (
            {"url": "https://github.com/acme/api"},
            {"github": "github.com"},
            AppServerRepository("acme/api", "github"),
        ),
        (
            {"url": "https://gitlab.example.com/team/api", "provider": "gitlab"},
            {"gitlab": "https://gitlab.example.com"},
            AppServerRepository("team/api", "gitlab"),
        ),
        (
            {"url": "https://ghe.example.com/acme/api", "provider": "github"},
            {"github": "https://ghe.example.com/api/v3"},
            AppServerRepository("acme/api", "github"),
        ),
        (
            {
                "url": "https://bitbucket.example.com/scm/dem/api.git",
                "provider": "bitbucket_data_center",
            },
            {"bitbucket_data_center": "bitbucket.example.com:7990"},
            AppServerRepository("dem/api", "bitbucket_data_center"),
        ),
        # A self-managed connection, a repository named on the public host: the
        # app server would look the name up on the connection's host instead.
        (
            {"url": "https://github.com/acme/api"},
            {"github": "ghe.example.com"},
            None,
        ),
        (
            {"url": "https://gitlab.com/group/web"},
            {"gitlab": "gitlab.example.com"},
            None,
        ),
        # A name without a host means the connected host.
        (
            {"url": "acme/api", "provider": "github"},
            {"github": "ghe.example.com"},
            AppServerRepository("acme/api", "github"),
        ),
        # An Azure DevOps connection records an organization, not a host.
        (
            {"url": "https://dev.azure.com/org/project/_git/repo"},
            {"azure_devops": "org"},
            AppServerRepository("org/project/repo", "azure_devops"),
        ),
    ],
)
def test_only_repositories_the_owner_is_connected_to_go_to_the_app_server(
    repo, connections, expected
):
    assert app_server_repository(repo, connections) == expected


def test_split_with_connections_keeps_order():
    repos = [
        {"url": "https://ghe.example.com/acme/api", "provider": "github"},
        {"url": "https://github.com/acme/public"},
        {"url": "acme/tools", "provider": "github"},
    ]
    split = split_repositories(repos, {"github": "ghe.example.com"})

    assert split.app_server == [
        AppServerRepository("acme/api", "github"),
        AppServerRepository("acme/tools", "github"),
    ]
    assert split.runner == [{"url": "https://github.com/acme/public"}]

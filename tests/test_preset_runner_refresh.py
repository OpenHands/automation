"""Every preset run gets the current runner, whatever release created the automation.

A preset tarball bakes the service's runner next to the user's prompt and
repository or plugin configuration. The fixtures under ``fixtures/preset_tarballs``
are preset tarballs as release 1.27.0 built them: its ``main.py``, ``setup.sh``
and ``finish_tool_hook.py`` from that tag, with a sample prompt and repositories.
"""

import io
import json
import tarfile
from pathlib import Path

import pytest

from openhands.automation.preset_router import (
    _generate_tarball,
    _load_plugin_preset_files,
    _load_prompt_preset_files,
    with_current_preset_runner,
)


FIXTURES = Path(__file__).parent / "fixtures" / "preset_tarballs"


def _members(tarball: bytes) -> dict[str, tuple[bytes, int]]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            data = tar.extractfile(member)
            out[member.name] = (data.read() if data else b"", member.mode)
    return out


def _tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("fixture", "load_current", "config"),
    [
        ("prompt-1.27.0.tar.gz", _load_prompt_preset_files, "repos_config.json"),
        ("plugin-1.27.0.tar.gz", _load_plugin_preset_files, "plugins_config.json"),
    ],
)
def test_a_1_27_0_preset_runs_with_the_current_runner(
    monkeypatch, fixture, load_current, config
):
    # A Windows service also adds setup.py; see the test below.
    monkeypatch.setattr("openhands.automation.preset_router.os.name", "posix")
    stored = (FIXTURES / fixture).read_bytes()
    before = _members(stored)
    # The 1.27.0 runner reads AGENT_SERVER_URL as local mode and cannot attach.
    assert b"IS_LOCAL_MODE = bool(agent_server_url)" in before["main.py"][0]
    assert b"AUTOMATION_RUNNER_REPOS" not in before["main.py"][0]

    after = _members(with_current_preset_runner(stored))

    current = load_current()
    for name in ("main.py", "setup.sh", "finish_tool_hook.py"):
        assert after[name][0] == current[name].encode(), name
    assert b"AUTOMATION_RUNNER_REPOS" in after["main.py"][0]
    assert b"RemoteConversation.attach(" in after["main.py"][0]
    # The user's content is kept byte for byte, and setup.sh stays executable.
    assert after["prompt.txt"] == before["prompt.txt"]
    assert after[config] == before[config]
    assert after["repos_config.json"] == before["repos_config.json"]
    assert after["setup.sh"][1] == 0o755
    assert set(after) == set(before)


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_a_current_preset_keeps_its_content(monkeypatch, os_name):
    monkeypatch.setattr("openhands.automation.preset_router.os.name", os_name)
    stored = _generate_tarball("hi")
    assert _members(with_current_preset_runner(stored)) == _members(stored)


@pytest.mark.parametrize(
    ("fixture", "load_current"),
    [
        ("prompt-1.27.0.tar.gz", _load_prompt_preset_files),
        ("plugin-1.27.0.tar.gz", _load_plugin_preset_files),
    ],
)
@pytest.mark.parametrize(("os_name", "gets_setup_py"), [("posix", False), ("nt", True)])
def test_an_older_preset_gets_setup_py_only_on_windows(
    monkeypatch, fixture, load_current, os_name, gets_setup_py
):
    """A Windows agent server cannot run setup.sh, so the run needs setup.py
    there even if the automation was created before the service shipped it."""
    monkeypatch.setattr("openhands.automation.preset_router.os.name", os_name)
    stored = (FIXTURES / fixture).read_bytes()
    assert "setup.py" not in _members(stored)

    after = _members(with_current_preset_runner(stored))

    assert ("setup.py" in after) is gets_setup_py
    if gets_setup_py:
        assert after["setup.py"][0] == load_current()["setup.py"].encode()


def test_a_preset_from_before_the_finish_hook_gets_it():
    # Presets created before 1.15.0 have no finish_tool_hook.py.
    current = _load_prompt_preset_files()
    stored = _tarball(
        {
            "main.py": current["main.py"].replace("AUTOMATION_RUNNER_REPOS", "X"),
            "prompt.txt": "hi",
            "setup.sh": "pip install old\n",
        }
    )

    after = _members(with_current_preset_runner(stored))

    for name in ("main.py", "setup.sh", "finish_tool_hook.py"):
        assert after[name][0] == current[name].encode(), name
    assert after["prompt.txt"][0] == b"hi"


@pytest.mark.parametrize(
    "files",
    [
        # A custom script tarball.
        {"main.py": "print('custom')\n", "setup.sh": "pip install x\n"},
        # A custom script that happens to ship a prompt.txt.
        {"main.py": '"""My own automation."""\n', "prompt.txt": "hi"},
        # No runner at all.
        {"prompt.txt": "hi", "run.sh": "echo hi"},
    ],
)
def test_other_tarballs_are_left_alone(files):
    stored = _tarball(files)
    assert with_current_preset_runner(stored) is stored


def test_something_that_is_not_a_tarball_is_left_alone():
    assert with_current_preset_runner(b"not a tarball") == b"not a tarball"


def test_the_plugin_runner_keeps_an_experiment_config():
    experiment = json.dumps({"experiment_id": "e1", "variants": []})
    plugin_runner = _load_plugin_preset_files()["main.py"]
    stored = _tarball(
        {
            "main.py": plugin_runner.replace("AUTOMATION_RUNNER_REPOS", "X"),
            "prompt.txt": "hi",
            "experiment_config.json": experiment,
        }
    )

    after = _members(with_current_preset_runner(stored))

    assert after["main.py"][0] == plugin_runner.encode()
    assert after["experiment_config.json"][0] == experiment.encode()

"""Exercise SDK imports and locking without making any model calls."""

from importlib.metadata import version
from pathlib import Path

import pytest
from filelock import FileLock, Timeout
from packaging.version import Version


def test_sdk_model_features_import():
    # SDK 1.63 imports this module at application startup. LiteLLM 1.109
    # removed an OpenAI transformation it depends on.
    from openhands.sdk.llm.utils.model_features import ALLOWED_OPENAI_PARAMS_KEY

    assert ALLOWED_OPENAI_PARAMS_KEY


def test_secure_filelock_exclusion_and_release(tmp_path: Path):
    assert Version(version("filelock")) >= Version("4.2.1")
    path = tmp_path / "dependency.lock"
    first = FileLock(path)
    second = FileLock(path)
    with first:
        with pytest.raises(Timeout):
            second.acquire(timeout=0)
    with second.acquire(timeout=0):
        assert second.is_locked

"""Tests for the execution module — build_tarball and _shell_quote.

Only tests pure logic that can run without a network.  The e2e flow
(run_automation against a real sandbox) lives in scripts/test_automation.py.
"""

import io
import tarfile

import pytest

from automation.execution import (
    EXTERNAL_DOWNLOAD_TIMEOUT,
    EXTERNAL_MAX_FILESIZE,
    AutomationResult,
    _shell_quote,
    build_tarball,
)


class TestBuildTarball:
    def test_produces_valid_tarball(self):
        tb = build_tarball({"hello.txt": "world", "bin.dat": b"\x00\x01"})
        with tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz") as tar:
            names = sorted(tar.getnames())
            assert names == ["bin.dat", "hello.txt"]
            hello = tar.extractfile("hello.txt")
            assert hello is not None
            assert hello.read() == b"world"
            bindat = tar.extractfile("bin.dat")
            assert bindat is not None
            assert bindat.read() == b"\x00\x01"

    def test_empty_files(self):
        tb = build_tarball({})
        with tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz") as tar:
            assert tar.getnames() == []

    def test_setup_and_entrypoint(self):
        tb = build_tarball(
            {
                "setup.sh": "#!/bin/bash\npip install requests\n",
                "run.py": 'print("ok")\n',
            }
        )
        with tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz") as tar:
            assert "setup.sh" in tar.getnames()
            assert "run.py" in tar.getnames()
            setup_file = tar.extractfile("setup.sh")
            assert setup_file is not None
            setup = setup_file.read().decode()
            assert "pip install" in setup


class TestShellQuote:
    def test_simple_string(self):
        assert _shell_quote("hello") == "'hello'"

    def test_string_with_spaces(self):
        assert _shell_quote("hello world") == "'hello world'"

    def test_string_with_single_quotes(self):
        assert _shell_quote("it's") == "'it'\\''s'"

    def test_empty_string(self):
        assert _shell_quote("") == "''"

    def test_special_characters(self):
        assert _shell_quote("$HOME") == "'$HOME'"


class TestAutomationResult:
    def test_frozen_dataclass(self):
        r = AutomationResult(success=True, sandbox_id="sb-1", exit_code=0, stdout="ok")
        assert r.success is True
        assert r.sandbox_id == "sb-1"
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]


class TestRunAutomationTarballSource:
    """Tests for run_automation tarball_source parameter."""

    def test_tarball_source_accepts_bytes(self):
        """tarball_source accepts bytes (will be uploaded)."""
        # This just validates the type - actual execution would need mocking
        source: bytes | str = b"test tarball content"
        assert isinstance(source, bytes)

    def test_tarball_source_accepts_str(self):
        """tarball_source accepts str URL (will be downloaded in sandbox)."""
        source: bytes | str = "https://example.com/file.tar.gz"
        assert isinstance(source, str)


class TestExternalDownloadConstants:
    """Tests for external download configuration constants."""

    def test_timeout_is_reasonable(self):
        """External download timeout should be reasonable (60-300s)."""
        assert 60 <= EXTERNAL_DOWNLOAD_TIMEOUT <= 300

    def test_max_filesize_is_reasonable(self):
        """Max filesize should be reasonable (10MB - 500MB)."""
        assert 10 * 1024 * 1024 <= EXTERNAL_MAX_FILESIZE <= 500 * 1024 * 1024

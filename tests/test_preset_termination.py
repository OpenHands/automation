"""Tests for graceful-termination cleanup in preset automations.

Covers:
- ``_termination.py`` ships in both prompt and plugin preset tarballs.
- ``_termination.py`` is valid Python and stdlib-only (importable standalone).
- The termination handlers fire a FAILED completion callback on SIGTERM, are
  idempotent, and stay quiet (no spurious FAILED) on a clean exit.
"""

import importlib.util
import io
import json
import signal
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from openhands.automation.preset_router import (
    _generate_plugin_tarball,
    _generate_tarball,
)
from openhands.sdk.plugin import PluginSource


PRESETS_DIR = Path(__file__).parent.parent / "openhands" / "automation" / "presets"
TERMINATION_PATH = PRESETS_DIR / "_termination.py"
PROMPT_SDK_MAIN = PRESETS_DIR / "prompt" / "sdk_main.py"
PLUGIN_SDK_MAIN = PRESETS_DIR / "plugin" / "sdk_main.py"


def _load_termination_module():
    """Load _termination.py as an isolated module (no package context needed)."""
    spec = importlib.util.spec_from_file_location(
        "preset_termination_under_test", TERMINATION_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTerminationFile:
    def test_termination_file_exists(self):
        assert TERMINATION_PATH.exists(), f"Not found: {TERMINATION_PATH}"

    def test_termination_file_valid_syntax(self):
        compile(TERMINATION_PATH.read_text(), str(TERMINATION_PATH), "exec")

    def test_termination_file_is_stdlib_only(self):
        """The helper must not import the OpenHands SDK so it loads in any env."""
        source = TERMINATION_PATH.read_text()
        # Disallow openhands imports anywhere in the module body.
        assert "import openhands" not in source
        assert "from openhands" not in source


class TestTerminationTarballInclusion:
    def test_prompt_tarball_includes_termination(self):
        tarball_bytes = _generate_tarball("do something")
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "_termination.py" in names
            # And it is the same content as the source file.
            extracted = tar.extractfile("_termination.py")
            assert extracted is not None
            assert extracted.read().decode() == TERMINATION_PATH.read_text()

    def test_plugin_tarball_includes_termination(self):
        tarball_bytes = _generate_plugin_tarball(
            [PluginSource(source="github:owner/repo")], "do something"
        )
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "_termination.py" in names

    def test_prompt_sdk_main_installs_handlers_and_marks_completed(self):
        source = PROMPT_SDK_MAIN.read_text()
        assert "from _termination import install_termination_handlers" in source
        assert "install_termination_handlers()" in source
        assert "mark_completed()" in source

    def test_plugin_sdk_main_installs_handlers_and_marks_completed(self):
        source = PLUGIN_SDK_MAIN.read_text()
        assert "from _termination import install_termination_handlers" in source
        assert "install_termination_handlers()" in source
        assert "mark_completed()" in source


class TestTerminationBehavior:
    """Behavioral tests for the stdlib termination helper."""

    def _fresh_module(self):
        """Load a freshly isolated copy of the module so _STATE is pristine."""
        mod = _load_termination_module()
        mod._STATE.update(
            installed=False,
            fired=False,
            callback_url="",
            callback_api_key="",
            run_id="",
        )
        return mod

    def _unregister_atexit(self, mod):
        """Remove any atexit handler our install registered for this module."""
        cb = mod._STATE.get("_atexit_cb")
        if cb is not None:
            try:
                mod.atexit.unregister(cb)
            except (AttributeError, ValueError):
                pass

    def test_install_is_idempotent(self):
        mod = self._fresh_module()
        register_calls = 0
        real_register = mod.atexit.register

        def counting_register(func, *args, **kwargs):
            nonlocal register_calls
            register_calls += 1
            return real_register(func, *args, **kwargs)

        with patch.object(mod.atexit, "register", side_effect=counting_register):
            mod.install_termination_handlers(
                callback_url="http://example.invalid/cb", run_id="r1"
            )
            first_count = register_calls
            # Second call must be a no-op (no duplicate atexit registration).
            mod.install_termination_handlers()
            assert register_calls == first_count
        self._unregister_atexit(mod)

    def test_clean_exit_does_not_fire_failure_callback(self):
        mod = self._fresh_module()
        mod.install_termination_handlers(callback_url="http://example.invalid/cb")
        with patch.object(mod.urllib.request, "urlopen") as mock_open:
            mod.mark_completed()
            # Simulate normal interpreter shutdown.
            mod._send_failure_callback(reason="interpreter shutdown")
            assert mock_open.call_count == 0, (
                "no FAILED callback should fire after a clean exit"
            )
        self._unregister_atexit(mod)

    def test_sigterm_fires_failure_callback_once(self):
        mod = self._fresh_module()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
            captured["url"] = req.full_url

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b"{}"

            return _Resp()

        mod.install_termination_handlers(
            callback_url="http://example.invalid/cb",
            callback_api_key="tok",
            run_id="r1",
        )
        with patch.object(
            mod.urllib.request, "urlopen", side_effect=fake_urlopen
        ) as mock_open:
            # The signal handler re-raises SystemExit to terminate the process;
            # catch it so the test can assert on the callback.
            with pytest.raises(SystemExit) as exc_info:
                mod._signal_handler(signal.SIGTERM, None)
            assert exc_info.value.code == 128 + signal.SIGTERM
            # And atexit firing during shutdown must be a no-op (already fired).
            mod._send_failure_callback(reason="interpreter shutdown")

        assert captured["body"]["status"] == "FAILED"
        assert captured["body"]["run_id"] == "r1"
        assert "terminated before completing" in captured["body"]["error"]
        assert captured["auth"] == "Bearer tok"
        # Idempotent: urlopen only called once despite signal + atexit.
        assert mock_open.call_count == 1
        self._unregister_atexit(mod)

    def test_missing_callback_url_does_not_raise(self):
        mod = self._fresh_module()
        mod.install_termination_handlers(callback_url="")
        with patch.object(mod.urllib.request, "urlopen") as mock_open:
            # No callback URL -> handler must be a no-op, not raise.
            with pytest.raises(SystemExit):
                mod._signal_handler(signal.SIGTERM, None)
            assert mock_open.call_count == 0
        self._unregister_atexit(mod)

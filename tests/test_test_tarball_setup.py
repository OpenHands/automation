from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_TARBALL_SETUP = ROOT / "scripts" / "test_tarball" / "setup.sh"
TEST_AUTOMATION_SCRIPT = ROOT / "scripts" / "test_automation.py"


def test_test_tarball_setup_fetches_service_sdk_version():
    content = TEST_TARBALL_SETUP.read_text()

    assert 'SDK_VERSION="1.22.0"' not in content
    assert "${AUTOMATION_API_URL}/sdk-version" in content
    assert "openhands-sdk==${SDK_VERSION}" in content


def test_test_tarball_setup_clears_ambient_uv_python_and_verifies_import():
    """Test tarball setup.sh pins the install to the run venv like the
    presets (#338): unset UV_PYTHON before `uv pip install` and verify the
    SDK is importable from the run venv before handing off."""
    content = TEST_TARBALL_SETUP.read_text()

    assert "unset UV_PYTHON" in content, (
        "setup.sh must unset UV_PYTHON so an ambient value cannot redirect "
        "the install out of the run venv (#338)"
    )
    assert "import openhands.sdk" in content, (
        "setup.sh must verify the SDK is importable from the run venv (#338)"
    )


def test_test_automation_runner_provides_automation_api_url():
    content = TEST_AUTOMATION_SCRIPT.read_text()

    assert '"AUTOMATION_API_URL": automation_api_url' in content
    assert "default_automation_api_url" in content

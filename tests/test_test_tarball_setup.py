from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_TARBALL_SETUP = ROOT / "scripts" / "test_tarball" / "setup.sh"
TEST_AUTOMATION_SCRIPT = ROOT / "scripts" / "test_automation.py"


def test_test_tarball_setup_fetches_service_sdk_version():
    content = TEST_TARBALL_SETUP.read_text()

    assert 'SDK_VERSION="1.22.0"' not in content
    assert "${AUTOMATION_API_URL}/sdk-version" in content
    assert "openhands-sdk==${SDK_VERSION}" in content


def test_test_automation_runner_provides_automation_api_url():
    content = TEST_AUTOMATION_SCRIPT.read_text()

    assert '"AUTOMATION_API_URL": automation_api_url' in content
    assert "default_automation_api_url" in content

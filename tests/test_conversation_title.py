"""Tests for deterministic preset automation conversation titles."""

from unittest.mock import MagicMock

from openhands.automation.presets.conversation_title import (
    MAX_CONVERSATION_TITLE_LENGTH,
    build_conversation_title,
    set_conversation_title,
)


class TestConversationTitle:
    """Conversation titles identify the automation, trigger, and individual run."""

    def test_cron_title_includes_schedule_and_run(self):
        context = {
            "automation_name": "Nightly repository review",
            "trigger": "cron",
            "trigger_payload": {"type": "cron", "schedule": "0 2 * * *"},
            "run_id": "12345678-1234-5678-1234-567812345678",
        }

        assert build_conversation_title(context) == (
            "Nightly repository review | cron 0 2 * * * | 12345678-123"
        )

    def test_github_event_title_includes_repository_and_pr(self):
        context = {
            "automation_name": "PR review",
            "trigger": "event",
            "trigger_payload": {"type": "event", "source": "github"},
            "event": {
                "repository": {"full_name": "OpenHands/automation"},
                "pull_request": {"number": 274},
            },
            "run_id": "abcdef01-2345-6789-abcd-ef0123456789",
        }

        assert build_conversation_title(context) == (
            "PR review | github OpenHands/automation#274 | abcdef01-234"
        )

    def test_title_normalizes_control_whitespace_and_preserves_unique_suffix(self):
        context = {
            "automation_name": "Review\n" + "x" * 300,
            "trigger": "cron",
            "trigger_payload": {"type": "cron", "schedule": "0\t9 * * 1"},
            "run_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }

        title = build_conversation_title(context)

        assert len(title) == MAX_CONVERSATION_TITLE_LENGTH
        assert "\n" not in title
        assert "\t" not in title
        assert title.endswith(" | cron 0 9 * * 1 | aaaaaaaa-bbb")

    def test_untrusted_event_fields_cannot_exceed_title_limit(self):
        context = {
            "automation_name": "Event review",
            "trigger": "event",
            "trigger_payload": {"type": "event", "source": "github"},
            "event": {
                "repository": {"full_name": "owner/" + "repository" * 100},
                "issue": {"number": 42},
            },
            "run_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        }

        title = build_conversation_title(context)

        assert len(title) <= MAX_CONVERSATION_TITLE_LENGTH
        assert title.endswith(" | bbbbbbbb-ccc")

    def test_missing_context_has_deterministic_fallback(self):
        assert build_conversation_title(None) == "Automation | run | unknown-run"

    def test_set_title_uses_agent_server_metadata_endpoint(self):
        response = MagicMock()
        workspace = MagicMock()
        workspace.client.patch.return_value = response
        context = {
            "automation_name": "Issue triage",
            "trigger": "event",
            "trigger_payload": {"type": "event", "source": "github"},
            "run_id": "12345678-1234-5678-1234-567812345678",
        }

        title = set_conversation_title(workspace, "conversation-id", context)

        assert title == "Issue triage | github | 12345678-123"
        workspace.client.patch.assert_called_once_with(
            "/api/conversations/conversation-id", json={"title": title}
        )
        response.raise_for_status.assert_called_once_with()

    def test_set_title_failure_does_not_abort_automation(self, capsys):
        workspace = MagicMock()
        workspace.client.patch.side_effect = OSError("agent server unavailable")

        result = set_conversation_title(workspace, "conversation-id", {})

        assert result is None
        assert "could not set conversation title" in capsys.readouterr().err

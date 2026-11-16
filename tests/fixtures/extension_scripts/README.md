These are the checks that scripts from
[OpenHands/extensions](https://github.com/OpenHands/extensions/tree/bb50764cf026b28c575414bb1269bfcecbc92b1a)
(`bb50764c`, release 0.40.0) use to decide whether they run on a local Agent
Canvas or on OpenHands Cloud or Enterprise. Each file holds the lines exactly as
they appear there, plus the imports they need. Automations keep the script files
they were created with, so these copies are what existing automations run.

| File | Copied from | Shipped in |
| --- | --- | --- |
| `github_client_is_cloud_run.py.txt` | `skills/github/scripts/github_client.py` lines 22-30 (the same in `plugins/github-comment-cleanup/scripts/github_client.py`) | catalog bundles github-pr-reviewer, github-issue-to-pr, github-agents-md-maintainer, github-delivery-watchdog, github-issue-triage, github-stale-ci-pr-closer, github-comment-cleanup |
| `is_cloud_flag.py.txt` | `skills/gitlab-issue-to-mr/scripts/main.py` lines 242-245 (the same in news-digest, github-agents-md-maintainer and slack-channel-monitor) | catalog bundles gitlab-issue-to-mr, github-agents-md-maintainer, news-digest |
| `is_local_flag.py.txt` | `skills/jira-issue-to-pr/scripts/main.py` lines 97-98 | the jira-issue-to-pr skill script |
| `agent_conversation_dispatcher.py.txt` | `skills/github/scripts/agent_conversation.py` lines 176-178 and 186, inside `AgentConversationDispatcher.__init__`; the class around them is reduced to these lines | catalog bundles github-pr-reviewer, github-issue-to-pr, github-issue-triage |
| `skill_custom_script_helpers.py.txt` | `skills/openhands-automation/SKILL.md`, the two helpers it tells agents to copy into every custom script | scripts agents write from the skill |

`test_cloud_run_env.py` runs each check against the environment a run gets. The
`.txt` suffix keeps the copies out of automatic Python rewrites.

# Plugin-backed automation E2E results

This document records local E2E validation for OpenHands/automation PR #580 using the companion SDK, Canvas, and extensions PRs.

## Revisions tested

| Repository | PR | Commit |
|---|---:|---|
| OpenHands/OpenHands | #18345 | `6ecf22e613bbf9a6aa00878ada856f1fc1fbd0da` |
| OpenHands/software-agent-sdk | #5987 | `b3aacc186f84f5093707863fb1e8828a7202b57f` |
| OpenHands/automation | #580 | `198abc9c814d38b283db8ef8e981dfa4c9b45b17` |
| OpenHands/extensions | #767 | `c0b0f6a80a54cc7b6d8c635275094aa0328cf82a` |

## Stack setup

Ran OpenHands Canvas PR #18345 locally on `http://127.0.0.1:12000` with:

- `OH_AGENT_SERVER_LOCAL_PATH=/workspace/project/software-agent-sdk`
- `OH_AUTOMATION_LOCAL_PATH=/workspace/project/automation`
- isolated `TMUX_TMPDIR=/tmp/oh-e2e-plugin-automation/tmux`

Readiness checks:

- Canvas root `/`: `HTTP 200`
- Agent server `/server_info`: `HTTP 200`
- Automation `/api/automation/health`: `HTTP 200`

Note: isolating `TMUX_TMPDIR` was necessary so the nested local agent-server did not interfere with the surrounding OpenHands runtime tmux session.

## Extensions plugin E2E

Created a plugin-spec automation from the extensions PR plugin:

```json
{
  "plugin": {
    "source": "github:OpenHands/extensions",
    "ref": "c0b0f6a80a54cc7b6d8c635275094aa0328cf82a",
    "repo_path": "plugins/hello-world-automation"
  },
  "config": {
    "message": "Hello from local e2e"
  }
}
```

Created automation:

- Automation ID: `d9082ca7-18ed-4708-a098-a700165ad836`
- Stored `plugin_spec.config`: `{"message": "Hello from local e2e"}`
- Trigger came from the manifest `suggested_trigger`
- Runner: `conversation`
- Entrypoint: `.venv/bin/python main.py`

Manual dispatch evidence:

| Run ID | Status | Evidence |
|---|---|---|
| `829fa03c-e990-49cb-b523-0be45cb6847f` | `FAILED` | Reached `RUNNING`, created bash command `68b6117387104e9ea19d69917489f7f5`, created conversation `b89f6372-cfbb-4f8b-ada3-a30c2bc54ed3`, fetched one plugin from the agent server. |
| `07af5b96-45d4-4fc0-bcb0-ac2a59bbdc37` | `FAILED` | Reached `RUNNING`, created bash command `70c921b94d2846359c2219e0af3598d4`, created conversation `94d842f0-7748-414b-a3ab-ee6f6443c214`, fetched one plugin from the agent server. |

Both runs failed after dispatch because the available local LLM credential was rejected by the provider:

```text
LLMAuthenticationError: Your LLM API key appears to be invalid or has expired. [kind=auth, source=environment]
```

The failure occurred after the plugin preflight, agent-server fetch, stock setup, bash command start, and conversation creation paths were exercised.

Relevant resolved runtime evidence:

```json
{
  "runner": "conversation",
  "plugin_resolved_ref": "c0b0f6a80a54cc7b6d8c635275094aa0328cf82a",
  "fetched_count": 1,
  "fetched_sources": ["github:OpenHands/extensions"]
}
```

Automation service and agent-server logs showed:

```text
POST /api/plugins/fetch HTTP/1.1" 200
Plugin automation payload built (10686 bytes) and 1 plugin(s) fetched
Automation dispatched successfully, waiting for callback
```

## Script-runner completion E2E

To verify the dispatch/callback path independently of LLM auth, a local script-runner plugin was created. The script read `AUTOMATION_CONFIG_PATH` and called `AUTOMATION_CALLBACK_URL` with `status=COMPLETED`.

Result:

- Automation ID: `b848dde2-ce56-431d-8e91-50fd78bfe1ed`
- Run ID: `7c7ed5a5-9249-41a1-b4ff-23bb10eff3df`
- Final status: `COMPLETED`
- Bash command ID: `b610a2e1eb6246009e9bb753d324e9b3`
- Error detail: `null`

## Extra JSON field behavior

Confirmed by E2E:

- Extra top-level fields in `.plugin/plugin.json` are accepted by SDK plugin preflight/fetch. The extensions plugin includes fields such as `homepage`, `repository`, `license`, and `keywords`; the local script plugin also included `extra_top_level`.
- Extra fields inside `.plugin/plugin.json.automation` are rejected by the automation schema.

Misconfigured automation block result:

```text
HTTP 422
Invalid automation plugin manifest: .plugin/plugin.json.automation: ... Extra inputs are not permitted
```

## Plugin dependency depth

Confirmed strict direct dependency fetch depth.

Local test shape:

```text
root plugin
└── runtime.plugins: [secondary plugin]
    └── automation.runtime.plugins: [/definitely/missing/nested-plugin]
```

The run completed and fetched only the root plugin plus the direct secondary plugin. The nested missing tertiary plugin was not fetched.

Resolved runtime evidence:

```json
{
  "fetched_count": 2,
  "fetched_sources": [
    "/tmp/oh-e2e-plugin-automation/nested-plugins/root-plugin",
    "/tmp/oh-e2e-plugin-automation/nested-plugins/secondary-plugin"
  ]
}
```

## Misconfiguration checks

All expected failures returned `HTTP 422`:

| Case | Result |
|---|---|
| Install config had an unexpected field while the plugin schema had `additionalProperties: false` | `Plugin automation config is invalid: Additional properties are not allowed ('unexpected' was unexpected)` |
| Bad plugin `repo_path` | `Failed to preflight automation plugin: repo_path not found: plugins/not-a-real-plugin` |
| Agent profile did not explicitly enable the plugin's allowed tool list | `Agent profile hello-world-agent must explicitly enable the tools allowed by this automation plugin: terminal` |
| Extra field inside `.plugin/plugin.json.automation` | `Extra inputs are not permitted` |

## Summary

Validated:

- Canvas PR #18345 can run with local SDK PR #5987 and automation PR #580 checkouts.
- Plugin-spec creation from the extensions PR manifest works.
- Install-time `config` is persisted and defaults are applied.
- Plugin source is fetched via `POST /api/plugins/fetch` on the agent server at dispatch time.
- The automation tarball does not include plugin source; runtime plugin paths come from agent-server fetch results.
- Direct plugin dependencies are fetched, but nested plugin dependencies are not recursively followed.
- Misconfigured plugin specs fail closed with actionable `422` errors.
- Script-runner plugin dispatch can complete successfully via callback.

This file was created by an AI agent (OpenHands) on behalf of the user as temporary PR evidence.

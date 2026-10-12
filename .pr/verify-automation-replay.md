# verify-automation: full replay

PR-only artifact (removed on approval).

Command, on this branch at commit 5c3e6ff:

```sh
export PATH="$PWD/.agents/skills/verify-automation/scripts:$PATH"
control-automation map run --all --fresh --record --keep-going --timeout 400
```

Each family ran on its own fresh local stack: the Automation Service in local
mode (SQLite) plus an Agent Server at the release `uv.lock` pins (1.54.0).
Model-backed bullets used DeepSeek v4.1 Flash through the Agent Server's
active profile (`llm preset deepseek`); total model spend was under one cent.
The whole replay took about 35 minutes.

**Result: ok. 255 pass, 8 xfail (known bugs), 2 blocked, 0 fail, 0 xpass.**

| Family | pass | xfail | blocked | fail |
|---|---|---|---|---|
| F01-service-status | 7 | 0 | 0 | 0 |
| F02-auth-and-cors | 18 | 0 | 0 | 0 |
| F03-capabilities-and-validate | 15 | 0 | 0 | 0 |
| F04-uploads | 13 | 0 | 0 | 0 |
| F05-automation-crud | 21 | 1 | 0 | 0 |
| F06-presets | 11 | 2 | 0 | 0 |
| F07-dispatch-and-runs | 16 | 0 | 0 | 0 |
| F08-cron-scheduling | 8 | 0 | 0 | 0 |
| F09-drafts | 19 | 1 | 0 | 0 |
| F10-custom-webhooks | 11 | 1 | 0 | 0 |
| F11-event-triggers | 21 | 0 | 0 | 0 |
| F12-kv-store | 28 | 3 | 0 | 0 |
| F13-git-sync | 30 | 0 | 0 | 0 |
| F14-watchdog-and-auto-disable | 14 | 0 | 0 | 0 |
| F15-telemetry-consent | 9 | 0 | 0 | 0 |
| F16-agent-server-contract | 9 | 0 | 0 | 0 |
| F17-stream-sources | 5 | 0 | 2 | 0 |

Pass counts include each family's `Preconditions:` block.

## Known bugs (xfail; each reproduced on at least two fresh runs)

| ID | Behaviour |
|---|---|
| `F05.patch-null` | `PATCH /v1/{id}` with explicit `null` for a NOT NULL field (`name`, `enabled`, ...) answers 500 (uncaught `IntegrityError`); `timeout: null` is stored, `state: null` is ignored. |
| `F06.continue-keep-alive` | Presets with `continue_conversation` do not force `keep_alive`, unlike raw create. |
| `F06.continue-keep-alive-false` | Presets accept `continue_conversation` with `keep_alive: false`; raw create answers 422. |
| `F09.template-after-draft-test` | After a test dispatch of a draft carrying `template.id`, `POST /v1` with that template answers 200 with the hidden DRAFT artifact instead of creating an active automation; `find_existing_template_automation` ignores `state`. |
| `F10.patch-null-required` | `PATCH /v1/webhooks/{id}` with `null` for `name`/`enabled` answers 500 (uncaught `IntegrityError`). |
| `F12.get-path-malformed` | `GET /v1/kv/{key}?path=a[b` answers 500. |
| `F12.patch-empty-path` | KV PATCH with an empty path answers 500 (`IndexError`). |
| `F12.jwt-unknown-automation` | A KV write with a valid token for a non-existent automation answers 500 (FK `IntegrityError`). |

## Blocked

- `F17.socket-mode-delivery`, `F17.identity-mismatch`: need a real Slack app
  (Socket Mode app token, bot token, a person to @-mention the bot). The
  recipes are written and read `$QA_SLACK_*`.

## Sample evidence

[`F07.preset-e2e.run.json`](F07.preset-e2e.run.json): the run record of a real
prompt-preset automation, dispatched through the API and finished by an
agent on DeepSeek Flash (`status: COMPLETED`, `conversation_id`, `cost`,
`finish_tool_response`). Secrets in evidence are redacted by the CLI.

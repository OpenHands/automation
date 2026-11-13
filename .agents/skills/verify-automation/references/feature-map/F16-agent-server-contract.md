# The run's contract with the Agent Server

What the Automation Service does on the Agent Server when it dispatches a
run, and what a run leaves there. In local mode the service uploads the
run's tarball and an env file to `/tmp/automation-{run_id}.tar.gz[.env]`
(`POST /api/file/upload`), then starts one bash chain
(`POST /api/bash/start_bash_command`) that sources and deletes the env
file, extracts the tarball into `{AUTOMATION_WORKSPACE_BASE}/automation-runs/{run_id}`,
deletes it, runs `setup.sh` if present and then the entrypoint, with the
automation's `timeout`. The returned command id is stored as the run's
`bash_command_id`; the watchdog later reads its `BashOutput`. A preset run's
`main.py` then talks to the same Agent Server through the SDK: it resolves
an LLM profile (the automation's `model`, else the active profile), creates a
conversation tagged with the run, titles it, and finishes through the
`finish` tool, whose arguments the service copies into
`run_metadata.finish_tool_response`. Every call carries
`X-Session-API-Key: $AUTOMATION_AGENT_SERVER_API_KEY`.

Source: `openhands/automation/execution.py`, `openhands/automation/backends/local.py`, `openhands/automation/dispatcher.py`, `openhands/automation/utils/agent_server.py`, `openhands/automation/utils/conversation_outcome.py`, `openhands/automation/presets/`, `openhands/automation/preset_router.py`

Routes: none

## Sub-features

- `F16.bash-command`: the run's `bash_command_id` names a `BashCommand` on the Agent Server whose command extracts `/tmp/automation-{run_id}.tar.gz` into the run directory, runs `bash run.sh` there and carries the automation's `timeout`; its `BashOutput` (searched with `command_id__eq`) has `exit_code` 0 and the script's stdout.
- `F16.uploads-cleaned`: after the run the uploaded tarball and env file are gone from `/tmp`, and the env file's values never appear in the command text.
- `F16.preset-conversation`: a preset run's conversation exists on the Agent Server with title `<automation name> — <UTC time>`, tags `automationid`, `automationname`, `automationrunid`, `automationtrigger`, its working directory set to the run directory, and `execution_status=finished`.
- `F16.finish-event`: the conversation contains the `finish` ActionEvent and the finish-tool hooks; the run's `finish_tool_response.message` equals the action's `message`, and the bash output ends with `ALL_OK` in LOCAL mode.
- `F16.model-profile`: an automation created with `model: deepseek-pro` runs its agent on that profile (`agent.llm.usage_id=profile:deepseek-pro`, model `deepseek/deepseek-v4-pro`).
- `F16.model-fallback`: an automation whose `model` names no profile still completes, on the Agent Server's active profile (deepseek-flash).
- `F16.wrong-key`: with a wrong `AUTOMATION_AGENT_SERVER_API_KEY` the dispatch fails at `start_bash_command` (401): the run is FAILED with `status_detail.source=agent_server`, `operation=execute_in_context`, no `bash_command_id`, and the automation stays enabled.
- `F16.unreachable`: with the Agent Server down the run is FAILED at dispatch (`All connection attempts failed`, `status_detail.source=agent_server`); a dispatch after it comes back succeeds.

## How to get to it (agent POV)

- The service's calls (all with `X-Session-API-Key`):
  `POST /api/file/upload?path=/tmp/automation-{run_id}.tar.gz` and the same
  for `...tar.gz.env`; `POST /api/bash/start_bash_command`
  `{command, timeout}` → `{id}` (= `bash_command_id`); the watchdog's
  `GET /api/bash/bash_events/search?kind__eq=BashOutput&command_id__eq=...&sort_order=TIMESTAMP_DESC&limit=1`;
  after a COMPLETED callback with a conversation id,
  `GET /api/conversations/{cid}/events/search?kind=openhands.sdk.event.llm_convertible.action.ActionEvent&sort_order=TIMESTAMP_DESC&limit=100`.
- The preset script's calls (`presets/prompt/sdk_main.py` via the SDK's
  `RemoteWorkspace` and `Conversation`): `GET /api/profiles/{model}` or
  `GET /api/settings` → active profile, `GET /api/skills`,
  `GET /api/settings/secrets`, `POST /api/conversations`,
  `PATCH /api/conversations/{id}` (title), then the conversation run.
- Recipes read the Agent Server with
  `api --agent-server` (for example `api --agent-server GET /api/conversations/{id}`) and
  `conversation events`. The SDK's `RemoteConversation` and the Agent
  Server's own REST routes are the consumer entry points; they belong to the
  Agent Server, not to this service.
- Config: `AUTOMATION_AGENT_SERVER_URL`, `AUTOMATION_AGENT_SERVER_API_KEY`,
  `AUTOMATION_SANDBOX_AGENT_SERVER_URL` (what the run sees as
  `AGENT_SERVER_URL`), `AUTOMATION_WORKSPACE_BASE`; the automation's
  `model` (becomes `AUTOMATION_MODEL`) and `timeout`.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`.
- The preset bullets (`F16.preset-conversation`, `F16.model-profile`,
  `F16.model-fallback`) need an LLM and PyPI; each saves the DeepSeek
  profiles itself (deepseek-flash active, deepseek-pro saved), so the other
  bullets, which use the stdlib `callback` fixture, run without credentials.

- **The bash command (`F16.bash-command`).** A callback fixture run, then its command and output on the Agent Server.
  ```sh
  BA=$(control-automation fixture automation --kind callback --name 'qa bash contract' --timeout 120 --field id)
  BR=$(control-automation api POST "/v1/$BA/dispatch" --expect 201 --field id)
  BID=$(control-automation run wait "$BA" --run-id "$BR" --status COMPLETED --timeout 60 --field bash_command_id)
  control-automation api --agent-server GET "/api/bash/bash_events/$BID" --expect 200 --check kind eq BashCommand --check id eq "$BID" --check timeout eq 120 --check command contains "tar xzf /tmp/automation-$BR.tar.gz -C " --check command contains "/automation-runs/$BR && rm -f /tmp/automation-$BR.tar.gz" --check command contains "([ ! -f setup.sh ] || bash setup.sh) && bash run.sh" --save F16.bash-command/command --quiet
  control-automation api --agent-server GET /api/bash/bash_events/search --query kind__eq=BashOutput --query command_id__eq="$BID" --query sort_order=TIMESTAMP_DESC --query limit=1 --expect 200 --check items len-eq 1 --check items.0.command_id eq "$BID" --check items.0.exit_code eq 0 --check items.0.stdout contains "qa callback run $BR" --save F16.bash-command/output --quiet
  ```
  The command id is the event id of the `BashCommand`; the `BashOutput`
  search is the one the watchdog makes.
- **Uploads cleaned from /tmp (`F16.uploads-cleaned`).** The chain removes both files it was handed.
  Requires: `F16.bash-command`
  ```sh
  control-automation api --agent-server GET "/api/bash/bash_events/$BID" --expect 200 --check command contains "/tmp/automation-$BR.tar.gz.env" --check command not-contains AUTOMATION_CALLBACK_URL --check command not-contains AUTOMATION_KV_TOKEN --quiet
  control-automation exec --expect-output gone -- sh -c "test ! -e /tmp/automation-$BR.tar.gz && test ! -e /tmp/automation-$BR.tar.gz.env && echo gone"
  control-automation state ls "workspaces/automation-runs/$BR" --expect run.sh --expect qa_lib.py --expect qa-env.json
  ```
  The command names the env file only by path; the values reach the run by
  sourcing it, and the extracted tarball sits in the run directory.
- **The preset run's conversation (`F16.preset-conversation`).** A prompt preset on deepseek-flash, then its conversation on the Agent Server.
  Needs: llm, pypi
  ```sh
  control-automation llm preset deepseek
  CA=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa contract preset","prompt":"Create a file named hello.txt in the current directory containing exactly: hi. Then call the finish tool. Do not do anything else.","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --field id)
  CR=$(control-automation api POST "/v1/$CA/dispatch" --expect 201 --field id)
  CID=$(control-automation run wait "$CA" --run-id "$CR" --status COMPLETED --timeout 900 --check run_metadata.finish_tool_response exists --save F16.preset-conversation/run --field conversation_id)
  control-automation api --agent-server GET "/api/conversations/$CID" --expect 200 --check title matches '^qa contract preset — [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8} UTC$' --check tags.automationrunid eq "$CR" --check tags.automationid eq "$CA" --check tags.automationname eq 'qa contract preset' --check tags.automationtrigger eq automation --check workspace.working_dir matches "/workspaces/automation-runs/$CR\$" --check execution_status eq finished --save F16.preset-conversation/conversation --quiet
  ```
  The conversation is kept after the run (`delete_on_close=False`), so it
  can be inspected afterwards.
- **The finish event (`F16.finish-event`).** What the agent's last action was, and what the service stored from it.
  Requires: `F16.preset-conversation`
  ```sh
  control-automation conversation events "$CID" --expect-kind ActionEvent --expect-kind HookExecutionEvent --expect-tool finish --save F16.finish-event/events
  FMSG=$(control-automation api GET "/v1/$CA/runs" --field runs.0.run_metadata.finish_tool_response.message)
  control-automation api --agent-server GET "/api/conversations/$CID/events/search" --query kind=openhands.sdk.event.llm_convertible.action.ActionEvent --query sort_order=TIMESTAMP_DESC --query limit=1 --expect 200 --check items.0.tool_name eq finish --check items.0.action.message eq "$FMSG" --save F16.finish-event/finish-action --quiet
  CBID=$(control-automation api GET "/v1/$CA/runs" --field runs.0.bash_command_id)
  control-automation api --agent-server GET /api/bash/bash_events/search --query kind__eq=BashOutput --query command_id__eq="$CBID" --expect 200 --check items.0.exit_code eq 0 --check items.0.stdout contains 'mode: LOCAL' --check items.0.stdout contains ALL_OK --quiet
  control-automation state cat "workspaces/automation-runs/$CR/hello.txt" --contains hi
  ```
  The finish-tool hooks (`SessionStart`, `PostToolUse`, `Stop`, `SessionEnd`) show up as
  `HookExecutionEvent`s; the stop hook refuses to end the conversation until
  `finish` has been used.
- **Named model profile (`F16.model-profile`).** `model` in the create body selects a saved Agent Server profile.
  Needs: llm, pypi
  ```sh
  control-automation llm preset deepseek
  MA=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa contract pro","model":"deepseek-pro","prompt":"Create a file named hello.txt in the current directory containing exactly: hi. Then call the finish tool. Do not do anything else.","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --check model eq deepseek-pro --field id)
  MR=$(control-automation api POST "/v1/$MA/dispatch" --expect 201 --field id)
  MCID=$(control-automation run wait "$MA" --run-id "$MR" --status COMPLETED --timeout 900 --field conversation_id)
  control-automation api --agent-server GET "/api/conversations/$MCID" --expect 200 --check agent.llm.usage_id eq profile:deepseek-pro --check agent.llm.model eq deepseek/deepseek-v4-pro --save F16.model-profile/conversation --quiet
  control-automation state cat "workspaces/automation-runs/$MR/hello.txt" --contains hi
  ```
  The active profile is still deepseek-flash; `AUTOMATION_MODEL=deepseek-pro`
  made the script load `GET /api/profiles/deepseek-pro` instead.
- **Unknown profile falls back (`F16.model-fallback`).** A `model` with no matching profile.
  Needs: llm, pypi
  ```sh
  control-automation llm preset deepseek
  GA=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa contract ghost","model":"qa-no-such-profile","prompt":"Create a file named hello.txt in the current directory containing exactly: hi. Then call the finish tool. Do not do anything else.","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --check model eq qa-no-such-profile --field id)
  GR=$(control-automation api POST "/v1/$GA/dispatch" --expect 201 --field id)
  GCID=$(control-automation run wait "$GA" --run-id "$GR" --status COMPLETED --timeout 900 --check status_detail eq null --field conversation_id)
  control-automation api --agent-server GET "/api/conversations/$GCID" --expect 200 --check agent.llm.usage_id eq profile:deepseek-flash --check agent.llm.model eq deepseek/deepseek-flash --save F16.model-fallback/conversation --quiet
  ```
  The Agent Server answers 404 for the profile and the script falls back to
  the active one; the service never validates `model` against the Agent
  Server's profiles.
- **Wrong Agent Server key (`F16.wrong-key`).** Restart the service with a wrong key; the dispatch fails before any command starts.
  ```sh
  control-automation restart --service automation --env AUTOMATION_AGENT_SERVER_API_KEY=wrong
  WA=$(control-automation fixture automation --kind callback --name 'qa wrong agent key' --field id)
  WR=$(control-automation api POST "/v1/$WA/dispatch" --expect 201 --field id)
  control-automation run wait "$WA" --run-id "$WR" --status FAILED --timeout 60 --check error_detail contains '401 Unauthorized' --check error_detail contains /api/bash/start_bash_command --check status_detail.source eq agent_server --check status_detail.operation eq execute_in_context --check status_detail.phase eq execution --check status_detail.transient eq false --check bash_command_id eq null --check started_at exists --save F16.wrong-key/run
  control-automation api GET "/v1/$WA" --expect 200 --check enabled eq true --check disabled_reason eq null --quiet
  control-automation exec --expect-output absent -- sh -c "test ! -e /tmp/automation-$WR.tar.gz && test ! -e /tmp/automation-$WR.tar.gz.env && echo absent"
  control-automation restart --service automation --reset-env
  WR2=$(control-automation api POST "/v1/$WA/dispatch" --expect 201 --field id)
  control-automation run wait "$WA" --run-id "$WR2" --status COMPLETED --timeout 60
  ```
  The uploads are refused too, but the service ignores upload results; the
  401 surfaces only from `start_bash_command`. With the right key restored
  the same automation completes.
- **Agent Server unreachable (`F16.unreachable`).** Stop the Agent Server, dispatch, then bring it back.
  ```sh
  UA=$(control-automation fixture automation --kind callback --name 'qa agent down' --field id)
  control-automation service stop agent-server
  UR=$(control-automation api POST "/v1/$UA/dispatch" --expect 201 --field id)
  control-automation run wait "$UA" --run-id "$UR" --status FAILED --timeout 60 --check error_detail eq 'All connection attempts failed' --check status_detail.source eq agent_server --check status_detail.operation eq execute_in_context --check status_detail.kind eq execution_error --check bash_command_id eq null --save F16.unreachable/run
  control-automation service start agent-server
  UR2=$(control-automation api POST "/v1/$UA/dispatch" --expect 201 --field id)
  control-automation run wait "$UA" --run-id "$UR2" --status COMPLETED --timeout 60
  ```

## Gotchas

- `kind__eq=BashCommand` combined with `command_id__eq` returns nothing
  (`BashCommand` events carry no `command_id`); read a command with
  `GET /api/bash/bash_events/{bash_command_id}` and its output with
  `kind__eq=BashOutput&command_id__eq=...`. The Agent Server writes one
  `BashOutput` with the exit code when the command ends.
- The agent's own terminal commands are in the same global bash history;
  always filter by the run's `bash_command_id`.
- Preset runs pip-install `openhands-sdk==<service's /sdk-version>` from
  PyPI in `setup.sh`; without PyPI they fail at setup with no callback and
  wait for the watchdog (F14). A warm run takes about 35 s.
- `model` defaults to null in local mode, and then the script uses the Agent
  Server's active profile; `llm preset deepseek` makes deepseek-flash
  active. Profile names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- The conversation's working directory is the run directory, so the agent
  sees `main.py`, `prompt.txt`, `setup.sh` and `.venv` next to the files it
  creates (`state ls` hides `.venv`).
- A wrong key or a dead Agent Server fails the run at dispatch with
  `transient=false`, but neither is a permanent kind, so they do not count
  toward the permanent auto-disable rule (F14).
- `service stop agent-server` kills every running bash chain of the run; a
  run dispatched before the stop and still RUNNING is left for the watchdog.

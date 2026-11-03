# Dispatch and run lifecycle

How an automation becomes a run and how that run reaches a terminal state.
`POST /v1/{id}/dispatch` creates a `PENDING` run with `trigger_source=manual`;
within one dispatcher interval the dispatcher marks it `RUNNING`, uploads the
tarball and an env file to the Agent Server and starts one bash chain there
(its id is the run's `bash_command_id`). The script inside the run reports
progress to `POST /v1/runs/{id}/phase` and its outcome to
`POST /v1/runs/{id}/complete`, using the URLs and key it receives as
environment variables. A caller can cancel a `PENDING` or `RUNNING` run.
`GET /v1/{id}/runs` is the only read view of runs: there is no
`GET /v1/runs/{id}`. What happens when no callback arrives (the watchdog) is
F14; what a run leaves on the Agent Server is F16.

Source: `openhands/automation/router.py`, `openhands/automation/dispatcher.py`, `openhands/automation/execution.py`, `openhands/automation/backends/local.py`, `openhands/automation/schemas.py`, `openhands/automation/utils/run.py`, `openhands/automation/utils/run_status_detail.py`, `openhands/automation/utils/callback_error.py`, `openhands/automation/utils/conversation_outcome.py`, `openhands/automation/observability.py`, `openhands/automation/presets/`

Routes: `POST /api/automation/v1/{automation_id}/dispatch`,
`GET /api/automation/v1/{automation_id}/runs`,
`POST /api/automation/v1/runs/{run_id}/complete`,
`POST /api/automation/v1/runs/{run_id}/phase`,
`POST /api/automation/v1/runs/{run_id}/cancel`

## Sub-features

- `F07.dispatch`: dispatching an automation answers 201 with a `PENDING` run (`trigger_source=manual`, `started_at`, `timeout_at`, `bash_command_id`, `conversation_id` all null), and the runs list shows it.
- `F07.dispatch-errors`: dispatch and the runs list answer 404 `Automation not found` for an unknown id, and 401 without a key.
- `F07.callback-lifecycle`: a run that calls back goes `PENDING` → `RUNNING` → `COMPLETED`; the stored run has `started_at`, `timeout_at`, `completed_at`, a `bash_command_id`, the last reported phase, `status_detail=null`, and `sandbox_id=null` (local mode).
- `F07.run-env`: the bash chain receives `AUTOMATION_RUN_ID`, `AUTOMATION_CALLBACK_URL`, `AUTOMATION_PHASE_URL`, `AUTOMATION_API_URL`, `AUTOMATION_TRIGGER_SOURCE=manual`, `AUTOMATION_EVENT_PAYLOAD`, `WORKSPACE_BASE` (the run directory), `AGENT_SERVER_URL`, a callback key and a KV token; `AUTOMATION_MODEL` only when the automation sets `model`.
- `F07.preset-e2e`: a prompt preset dispatched on DeepSeek flash completes through its own callback with a `conversation_id`, a `cost`, `run_metadata.finish_tool_response`, and the file the prompt asked for in the run directory.
- `F07.phase`: `POST /v1/runs/{id}/phase` sets `current_phase` while the run is `PENDING` or `RUNNING`, collapses whitespace and control characters, and answers 422 for an empty, blank or over-200-character phase and 404 for an unknown run.
- `F07.complete-fields`: a COMPLETED callback stores `conversation_id` and `cost`; `task_outcome` is accepted and ignored; an unknown conversation leaves `run_metadata` null.
- `F07.complete-failed`: a FAILED callback without `error` stores `Completion callback reported failure`; with a string error it stores the string, and `status_detail` is `{phase: callback, kind: execution_error, source: sdk_callback}`.
- `F07.complete-structured-error`: a FAILED callback with a structured error and classification stores `error_detail` `CODE: detail [kind=..., source=...]` and copies `kind`, `code`, `source` and `user_action` into `status_detail`.
- `F07.complete-validation`: `/complete` answers 422 for an unknown key or a status other than COMPLETED/FAILED, 404 `Run not found` for an unknown run, and 401 `Invalid API key` for a wrong key.
- `F07.complete-conflict`: `/complete`, `/phase` and `/cancel` on a run that is already terminal answer 409 with the run's status in the detail.
- `F07.cancel-pending`: cancelling a `PENDING` run answers 200 `CANCELLED` (`error_detail=Cancelled by user`, `completed_at` set, `started_at` null); `/complete` on a PENDING run is 409, and the dispatcher never starts a cancelled run.
- `F07.cancel-running`: cancelling a `RUNNING` run answers 200 `CANCELLED`, but in local mode the bash chain keeps running on the Agent Server, and its late callback gets 409 `Run is CANCELLED, expected RUNNING`.
- `F07.cancel-errors`: cancelling an unknown run is 404 `Run not found`; cancelling twice is 409.
- `F07.runs-list`: the runs list is newest first, pages with `limit` (1..100) and `offset`, and reports `total` and `status_counts` over the automation's whole history regardless of paging; out-of-range paging is 422.
- `F07.disabled-manual`: a manual dispatch on a disabled (`enabled=false`, `INACTIVE`) automation still runs to completion.

## How to get to it (agent POV)

- REST: the five routes above. Agent Canvas's "Run now" button and the
  OpenHands extensions' `automation` skill call `POST /v1/{id}/dispatch` and
  poll `GET /v1/{id}/runs`; there is no per-run GET.
- Inside a run (the consumer of `/phase` and `/complete`):
  - the preset `setup.sh` posts `{"phase":"Installing dependencies"}` to
    `$AUTOMATION_PHASE_URL`; `main.py` posts later phases ("Agent is working
    on the task", then summaries of agent actions);
  - the SDK's workspace context (`openhands-sdk` `workspace/base.py`,
    `_send_completion_callback`) posts `{status, run_id, conversation_id,
    cost, error?}` to `$AUTOMATION_CALLBACK_URL` with
    `Authorization: Bearer $AUTOMATION_CALLBACK_API_KEY` on exit;
  - the stdlib fixtures do the same with `qa_lib.py phase|complete`
    (`fixture automation --kind callback|fail-callback|sleep`), and the
    recipes below also call `/phase` and `/complete` directly with the local
    key, which owns every local automation.
- Config: `AUTOMATION_DISPATCHER_INTERVAL_SECONDS` (launch shrinks it to 2 s),
  `AUTOMATION_BASE_URL` (builds the callback and phase URLs),
  `AUTOMATION_LOCAL_API_KEY` (becomes `AUTOMATION_CALLBACK_API_KEY`),
  `AUTOMATION_WORKSPACE_BASE` (run directories under `automation-runs/`).

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. Only `F07.preset-e2e`
  needs an LLM and PyPI; its bullet sets the DeepSeek profiles itself.
  Everything else uses the stdlib fixtures.

- **Manual dispatch (`F07.dispatch`).** A new run is PENDING with nothing started yet.
  ```sh
  DA=$(control-automation fixture automation --kind callback --name 'qa dispatch' --field id)
  DR=$(control-automation api POST "/v1/$DA/dispatch" --expect 201 --check status eq PENDING --check trigger_source eq manual --check automation_id eq "$DA" --check started_at eq null --check timeout_at eq null --check bash_command_id eq null --check conversation_id eq null --save F07.dispatch/dispatch --field id)
  control-automation api GET "/v1/$DA/runs" --expect 200 --check total eq 1 --check runs.0.id eq "$DR" --quiet
  control-automation run wait "$DA" --run-id "$DR" --status COMPLETED --timeout 60
  ```
  The 201 body is the run; the runs list shows the same id, and it completes
  shortly after (the callback fixture).
- **Dispatch errors (`F07.dispatch-errors`).** Unknown ids and missing keys.
  ```sh
  control-automation api POST /v1/00000000-0000-0000-0000-000000000000/dispatch --expect 404 --check detail eq 'Automation not found' --save F07.dispatch-errors/unknown
  control-automation api GET /v1/00000000-0000-0000-0000-000000000000/runs --expect 404 --check detail eq 'Automation not found'
  EA=$(control-automation fixture automation --kind callback --name 'qa dispatch auth' --field id)
  control-automation api POST "/v1/$EA/dispatch" --auth none --expect 401
  control-automation api GET "/v1/$EA/runs" --expect 200 --check total eq 0 --quiet
  ```
  The refused dispatch created no run.
- **Callback lifecycle (`F07.callback-lifecycle`).** The callback fixture reports a phase, then COMPLETED.
  ```sh
  LA=$(control-automation fixture automation --kind callback --name 'qa lifecycle' --field id)
  LR=$(control-automation api POST "/v1/$LA/dispatch" --expect 201 --field id)
  control-automation run wait "$LA" --run-id "$LR" --status COMPLETED --timeout 60 --check started_at exists --check timeout_at exists --check completed_at exists --check bash_command_id exists --check current_phase eq 'qa: running' --check status_detail eq null --check error_detail eq null --check sandbox_id eq null --check conversation_id eq null --save F07.callback-lifecycle/run
  control-automation api GET "/v1/$LA/runs" --expect 200 --check runs.0.status eq COMPLETED --check status_counts.COMPLETED eq 1 --quiet
  ```
  `run wait` records the history (`RUNNING`, phase `Preparing environment`,
  then `COMPLETED`); the fixture's `qa: running` phase stays as the run's
  last phase after it terminates.
- **Environment handed to the run (`F07.run-env`).** The callback fixture dumps its environment to `qa-env.json` in the run directory (secrets shown as `<present>`).
  ```sh
  VA=$(control-automation fixture automation --kind callback --name 'qa env' --field id)
  VR=$(control-automation api POST "/v1/$VA/dispatch" --expect 201 --field id)
  control-automation run wait "$VA" --run-id "$VR" --status COMPLETED --timeout 60
  control-automation state cat "workspaces/automation-runs/$VR/qa-env.json" --check AUTOMATION_RUN_ID eq "$VR" --check AUTOMATION_ID eq "$VA" --check AUTOMATION_NAME eq 'qa env' --check AUTOMATION_TRIGGER_SOURCE eq manual --check AUTOMATION_RUN_TRIGGER_SOURCE eq manual --check AUTOMATION_TRIGGER_TYPE eq cron --check AUTOMATION_CALLBACK_URL matches "/api/automation/v1/runs/$VR/complete\$" --check AUTOMATION_PHASE_URL matches "/api/automation/v1/runs/$VR/phase\$" --check AUTOMATION_API_URL matches '/api/automation$' --check AUTOMATION_EVENT_PAYLOAD contains "\"automation_id\": \"$VA\"" --check WORKSPACE_BASE matches "/workspaces/automation-runs/$VR\$" --check AGENT_SERVER_URL matches '^http://' --check AUTOMATION_CALLBACK_API_KEY eq '<present>' --check AUTOMATION_KV_TOKEN eq '<present>' --check AUTOMATION_MODEL missing
  VM=$(control-automation fixture automation --kind callback --name 'qa env model' --extra-json '{"model":"deepseek-pro"}' --field id)
  VMR=$(control-automation api POST "/v1/$VM/dispatch" --expect 201 --field id)
  control-automation run wait "$VM" --run-id "$VMR" --status COMPLETED --timeout 60
  control-automation state cat "workspaces/automation-runs/$VMR/qa-env.json" --check AUTOMATION_MODEL eq deepseek-pro
  ```
- **Preset run on DeepSeek (`F07.preset-e2e`).** A real agent creates `hello.txt` and finishes.
  Needs: llm, pypi
  ```sh
  control-automation llm preset deepseek
  PA=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa preset hello","prompt":"Create a file named hello.txt in the current directory containing exactly: hi. Then call the finish tool. Do not do anything else.","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --field id)
  PR=$(control-automation api POST "/v1/$PA/dispatch" --expect 201 --field id)
  control-automation run wait "$PA" --run-id "$PR" --status COMPLETED --timeout 900 --check conversation_id exists --check cost ge 0 --check run_metadata.finish_tool_response exists --check bash_command_id exists --check status_detail eq null --save F07.preset-e2e/run
  control-automation state cat "workspaces/automation-runs/$PR/hello.txt" --contains hi
  control-automation state ls "workspaces/automation-runs/$PR" --expect main.py --expect prompt.txt --expect setup.sh --expect hello.txt
  ```
  About 35 s on a warm machine (setup installs the SDK from PyPI). The run's
  phases go `Installing dependencies`, `Agent is working on the task`, then
  agent summaries; `run_metadata.finish_tool_response` holds the arguments of
  the agent's `finish` call (`summary`, `message`, `status`,
  `outcome_summary`). F16 inspects the conversation itself.
- **Phase reporting (`F07.phase`).** Against a sleeping run, which stays RUNNING.
  ```sh
  HA=$(control-automation fixture automation --kind sleep --name 'qa phase' --timeout 60 --field id)
  HR=$(control-automation api POST "/v1/$HA/dispatch" --expect 201 --field id)
  control-automation api GET "/v1/$HA/runs" --until-ok 60 --check runs.0.status eq RUNNING --check runs.0.current_phase eq 'qa: sleeping' --quiet
  control-automation api POST "/v1/runs/$HR/phase" --json '{"phase":"  qa\n\tphase   two  "}' --expect 200 --check current_phase eq 'qa phase two' --check status eq RUNNING --save F07.phase/normalized --quiet
  control-automation api GET "/v1/$HA/runs" --check runs.0.current_phase eq 'qa phase two' --quiet
  control-automation api POST "/v1/runs/$HR/phase" --json '{"phase":""}' --expect 422
  control-automation api POST "/v1/runs/$HR/phase" --json '{"phase":" \n\t "}' --expect 422 --check detail.0.type eq string_too_short
  LONG=$(python3 -c 'print("x" * 201)')
  control-automation api POST "/v1/runs/$HR/phase" --json "{\"phase\":\"$LONG\"}" --expect 422 --check detail.0.type eq string_too_long --quiet
  control-automation api POST /v1/runs/00000000-0000-0000-0000-000000000000/phase --json '{"phase":"x"}' --expect 404 --check detail eq 'Run not found'
  control-automation api GET "/v1/$HA/runs" --check runs.0.current_phase eq 'qa phase two' --quiet
  control-automation api POST "/v1/runs/$HR/cancel" --expect 200
  ```
  Refused phases leave the stored phase unchanged. The run is cancelled at
  the end so nothing waits on it.
- **Callback fields (`F07.complete-fields`).** Complete a sleeping run directly with a conversation id and a cost.
  ```sh
  FA=$(control-automation fixture automation --kind sleep --name 'qa complete fields' --timeout 60 --field id)
  FR=$(control-automation api POST "/v1/$FA/dispatch" --expect 201 --field id)
  control-automation run wait "$FA" --run-id "$FR" --status RUNNING --timeout 60
  control-automation api POST "/v1/runs/$FR/complete" --json "{\"status\":\"COMPLETED\",\"run_id\":\"$FR\",\"conversation_id\":\"qa-conversation-1\",\"cost\":0.25,\"task_outcome\":{\"qa\":1}}" --expect 200 --check status eq COMPLETED --check conversation_id eq qa-conversation-1 --check cost eq 0.25 --check run_metadata eq null --check completed_at exists --save F07.complete-fields/complete
  control-automation api GET "/v1/$FA/runs" --check runs.0.status eq COMPLETED --check runs.0.conversation_id eq qa-conversation-1 --check runs.0.cost eq 0.25 --quiet
  ```
  The service looks the conversation up on the Agent Server for a `finish`
  call; an unknown id just leaves `run_metadata` null.
- **FAILED callbacks (`F07.complete-failed`).** Without an error, and with a plain string (the `fail-callback` fixture).
  ```sh
  NA=$(control-automation fixture automation --kind sleep --name 'qa failed bare' --timeout 60 --field id)
  NR=$(control-automation api POST "/v1/$NA/dispatch" --expect 201 --field id)
  control-automation run wait "$NA" --run-id "$NR" --status RUNNING --timeout 60
  control-automation api POST "/v1/runs/$NR/complete" --json '{"status":"FAILED"}' --expect 200 --check status eq FAILED --check error_detail eq 'Completion callback reported failure' --check status_detail.phase eq callback --check status_detail.source eq sdk_callback --save F07.complete-failed/bare --quiet
  SA=$(control-automation fixture automation --kind fail-callback --name 'qa failed string' --field id)
  SR=$(control-automation api POST "/v1/$SA/dispatch" --expect 201 --field id)
  control-automation run wait "$SA" --run-id "$SR" --status FAILED --timeout 60 --check error_detail eq 'qa: deliberate failure' --check status_detail.phase eq callback --check status_detail.kind eq execution_error --check status_detail.source eq sdk_callback --check status_detail.transient eq false --check completed_at exists --save F07.complete-failed/string
  ```
- **Structured callback error (`F07.complete-structured-error`).** The shape the SDK sends for a classified failure.
  ```sh
  TA=$(control-automation fixture automation --kind sleep --name 'qa failed structured' --timeout 60 --field id)
  TR=$(control-automation api POST "/v1/$TA/dispatch" --expect 201 --field id)
  control-automation run wait "$TA" --run-id "$TR" --status RUNNING --timeout 60
  control-automation api POST "/v1/runs/$TR/complete" --json '{"status":"FAILED","error":{"source":"environment","code":"QA_AUTH","detail":"qa bad key","classification":{"kind":"auth","retryable":false,"user_action":"settings"}}}' --expect 200 --check error_detail eq 'QA_AUTH: qa bad key [kind=auth, source=environment]' --check status_detail.kind eq auth --check status_detail.code eq QA_AUTH --check status_detail.source eq environment --check status_detail.user_action eq settings --check status_detail.detail eq 'qa bad key' --check status_detail.transient eq false --save F07.complete-structured-error/complete --quiet
  control-automation api GET "/v1/$TA/runs" --check runs.0.status eq FAILED --check runs.0.status_detail.kind eq auth --quiet
  ```
  A permanent kind like `auth` counts toward auto-disable (F14).
- **Callback validation (`F07.complete-validation`).** Bad bodies, unknown runs, wrong keys.
  Requires: `F07.complete-structured-error`
  ```sh
  control-automation api POST "/v1/runs/$TR/complete" --json '{"status":"COMPLETED","qa_extra":1}' --expect 422 --check detail.0.type eq extra_forbidden --save F07.complete-validation/extra
  control-automation api POST "/v1/runs/$TR/complete" --json '{"status":"DONE"}' --expect 422 --quiet
  control-automation api POST "/v1/runs/$TR/complete" --json '{}' --expect 422 --quiet
  control-automation api POST /v1/runs/00000000-0000-0000-0000-000000000000/complete --json '{"status":"COMPLETED"}' --expect 404 --check detail eq 'Run not found'
  control-automation api POST "/v1/runs/$TR/complete" --auth bad --json '{"status":"COMPLETED"}' --expect 401 --check detail eq 'Invalid API key'
  control-automation api GET "/v1/$TA/runs" --check runs.0.status eq FAILED --quiet
  ```
  None of them changed the stored run.
- **Terminal runs refuse callbacks (`F07.complete-conflict`).** The FAILED run from the structured-error bullet.
  Requires: `F07.complete-structured-error`
  ```sh
  control-automation api POST "/v1/runs/$TR/complete" --json '{"status":"COMPLETED"}' --expect 409 --check detail eq 'Run is FAILED, expected RUNNING' --save F07.complete-conflict/complete
  control-automation api POST "/v1/runs/$TR/phase" --json '{"phase":"late"}' --expect 409 --check detail eq 'Run is FAILED, expected PENDING or RUNNING'
  control-automation api POST "/v1/runs/$TR/cancel" --expect 409 --check detail eq 'Run is FAILED, only PENDING or RUNNING runs can be cancelled'
  control-automation api GET "/v1/$TA/runs" --check runs.0.status eq FAILED --check runs.0.current_phase eq 'qa: sleeping' --quiet
  ```
- **Cancel a pending run (`F07.cancel-pending`).** Restart with a one-hour dispatcher interval so new runs stay PENDING, cancel one, then restore the interval and watch only the other run execute.
  ```sh
  control-automation restart --service automation --env AUTOMATION_DISPATCHER_INTERVAL_SECONDS=3600
  QA=$(control-automation fixture automation --kind callback --name 'qa cancel pending' --field id)
  QR1=$(control-automation api POST "/v1/$QA/dispatch" --expect 201 --field id)
  QR2=$(control-automation api POST "/v1/$QA/dispatch" --expect 201 --field id)
  control-automation api POST "/v1/runs/$QR1/phase" --json '{"phase":"qa queued"}' --expect 200 --check status eq PENDING --check current_phase eq 'qa queued' --quiet
  control-automation api POST "/v1/runs/$QR1/complete" --json '{"status":"COMPLETED"}' --expect 409 --check detail eq 'Run is PENDING, expected RUNNING'
  control-automation api POST "/v1/runs/$QR1/cancel" --expect 200 --check status eq CANCELLED --check error_detail eq 'Cancelled by user' --check completed_at exists --check started_at eq null --check status_detail eq null --save F07.cancel-pending/cancel --quiet
  control-automation restart --service automation --reset-env
  control-automation run wait "$QA" --run-id "$QR2" --status COMPLETED --timeout 60
  control-automation api GET "/v1/$QA/runs" --expect 200 --check total eq 2 --check status_counts.CANCELLED eq 1 --check status_counts.COMPLETED eq 1 --save F07.cancel-pending/runs --quiet
  control-automation run wait "$QA" --run-id "$QR1" --status CANCELLED --timeout 5 --check started_at eq null --check bash_command_id eq null
  ```
  A PENDING run accepts a phase but not a completion. After the restart the
  dispatcher picks up the other run at once; the cancelled one never gets a
  `started_at` or a `bash_command_id`.
- **Cancel a running run (`F07.cancel-running`).** The run is CANCELLED, but its bash chain keeps running on the Agent Server.
  ```sh
  CA=$(control-automation fixture automation --kind sleep --name 'qa cancel running' --timeout 60 --field id)
  CR=$(control-automation api POST "/v1/$CA/dispatch" --expect 201 --field id)
  control-automation api GET "/v1/$CA/runs" --until-ok 60 --check runs.0.status eq RUNNING --check runs.0.current_phase eq 'qa: sleeping' --quiet
  control-automation api POST "/v1/runs/$CR/cancel" --expect 200 --check status eq CANCELLED --check error_detail eq 'Cancelled by user' --check completed_at exists --save F07.cancel-running/cancel --quiet
  control-automation exec --expect-output "automation-runs/$CR" -- sh -c "pgrep -af '[a]utomation-runs/$CR'"
  control-automation api POST "/v1/runs/$CR/complete" --json '{"status":"COMPLETED"}' --expect 409 --check detail eq 'Run is CANCELLED, expected RUNNING' --save F07.cancel-running/late-callback
  control-automation api GET "/v1/$CA/runs" --check runs.0.status eq CANCELLED --quiet
  ```
  `pgrep` finds the run's bash chain still alive after the cancel (local mode
  has no sandbox to delete and sends no stop); it ends at its own 60 s
  timeout. The late COMPLETED callback is refused and the run stays
  CANCELLED.
- **Cancel errors (`F07.cancel-errors`).** Unknown run, and a second cancel.
  Requires: `F07.cancel-running`
  ```sh
  control-automation api POST /v1/runs/00000000-0000-0000-0000-000000000000/cancel --expect 404 --check detail eq 'Run not found'
  control-automation api POST "/v1/runs/$CR/cancel" --expect 409 --check detail eq 'Run is CANCELLED, only PENDING or RUNNING runs can be cancelled' --save F07.cancel-errors/twice
  ```
- **Runs list paging and counts (`F07.runs-list`).** Three runs of one automation: two COMPLETED, one FAILED.
  ```sh
  RA=$(control-automation fixture automation --kind sleep --name 'qa runs list' --timeout 60 --field id)
  for OUTCOME in COMPLETED FAILED COMPLETED; do
    RLR=$(control-automation api POST "/v1/$RA/dispatch" --expect 201 --field id)
    control-automation run wait "$RA" --run-id "$RLR" --status RUNNING --timeout 60
    control-automation api POST "/v1/runs/$RLR/complete" --json "{\"status\":\"$OUTCOME\"}" --expect 200 --quiet
  done
  control-automation api GET "/v1/$RA/runs" --expect 200 --check total eq 3 --check runs len-eq 3 --check runs.0.id eq "$RLR" --check status_counts.COMPLETED eq 2 --check status_counts.FAILED eq 1 --check status_counts.PENDING missing --save F07.runs-list/all --quiet
  control-automation api GET "/v1/$RA/runs" --query limit=1 --expect 200 --check runs len-eq 1 --check runs.0.id eq "$RLR" --check total eq 3 --check status_counts.COMPLETED eq 2 --quiet
  control-automation api GET "/v1/$RA/runs" --query limit=1 --query offset=1 --expect 200 --check runs len-eq 1 --check runs.0.id ne "$RLR" --check total eq 3 --quiet
  control-automation api GET "/v1/$RA/runs" --query offset=3 --expect 200 --check runs len-eq 0 --check total eq 3 --check status_counts.FAILED eq 1 --quiet
  control-automation api GET "/v1/$RA/runs" --query limit=0 --expect 422 --quiet
  control-automation api GET "/v1/$RA/runs" --query limit=101 --expect 422 --quiet
  control-automation api GET "/v1/$RA/runs" --query offset=-1 --expect 422 --quiet
  control-automation api GET "/v1/$RA/runs" --query limit=100 --expect 200 --check total eq 3 --quiet
  ```
  `status_counts` lists only statuses that occur, and both it and `total`
  ignore `limit` and `offset`.
- **Manual dispatch ignores `enabled` (`F07.disabled-manual`).** Disable the automation, dispatch, and the run still executes.
  ```sh
  MA=$(control-automation fixture automation --kind callback --name 'qa disabled manual' --field id)
  control-automation api PATCH "/v1/$MA" --json '{"enabled":false}' --expect 200 --check enabled eq false --quiet
  control-automation api GET "/v1/$MA" --expect 200 --check enabled eq false --check state eq INACTIVE --quiet
  MR=$(control-automation api POST "/v1/$MA/dispatch" --expect 201 --check trigger_source eq manual --field id)
  control-automation run wait "$MA" --run-id "$MR" --status COMPLETED --timeout 60 --check bash_command_id exists --save F07.disabled-manual/run
  control-automation api GET "/v1/$MA" --expect 200 --check enabled eq false --check state eq INACTIVE --quiet
  ```
  The dispatcher picks up manual runs regardless of `enabled`/`state`; only
  scheduled and event runs are held back (they become `SKIPPED`). Running it
  does not re-enable the automation.

## Gotchas

- There is no `GET /v1/runs/{id}`: poll `GET /v1/{id}/runs` (`run wait`
  does). `created_at` has one-second resolution, so runs created in the same
  second have no guaranteed order; `run wait --run-id` avoids depending on it.
- A run that calls back within a second passes through `RUNNING` faster than
  a poll can see; assert `started_at`, `bash_command_id` and the history
  instead, or use the `sleep` fixture when RUNNING itself is under test.
- `run wait --status RUNNING --check current_phase eq ...` keeps polling until
  the checks hold, which is how to wait for the bash chain to really start;
  a terminal status returns at once. Polling the list with
  `api GET /v1/{id}/runs --until-ok N --check ...` works too.
- The phase sequence: the dispatcher writes `Preparing environment` when it
  marks the run RUNNING and `Starting automation` once bash starts; after
  that the script's own phases overwrite it, and the last one survives the
  terminal transition.
- `blocking_factor` and `task_outcome` in `/complete` are accepted and
  ignored; the outcome the agent reported lives in
  `run_metadata.finish_tool_response`, which the service fetches from the
  Agent Server's conversation events (best effort, null on any failure).
- Cancelling a RUNNING run in local mode does not stop its bash chain: the
  script keeps running, may still write into the run directory, and its
  callback gets 409. In cloud mode the sandbox is deleted instead.
- Callbacks authenticate with `AUTOMATION_LOCAL_API_KEY` (handed to the run
  as `AUTOMATION_CALLBACK_API_KEY`); the local user owns every automation, so
  the 403 `Not your automation` branch is unreachable with one local key.
- A DRAFT artifact automation refuses `POST /v1/{id}/dispatch` with 409; that
  guard is mapped in F09 (`F09.artifact-guard`).
- `restart --service automation --env` adds to the env of every later
  restart until `--reset-env`; the cancel-pending bullet restores it before
  the dispatcher is needed again.

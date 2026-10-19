# Watchdog, timeouts and auto-disable

The background actors that finish runs nobody reported on, and that pause
automations that keep failing. Every `AUTOMATION_WATCHDOG_INTERVAL_SECONDS`
(10 s under `launch`) the watchdog takes each `RUNNING` run whose
`timeout_at` has passed and asks the Agent Server for the last `BashOutput`
of the run's `bash_command_id`: exit 0 means COMPLETED (the callback was
missed), exit -1 means the Agent Server killed it at the automation's
`timeout` (FAILED, `Timed out: ...`), any other exit code is FAILED
`exit_code=N`, a command still running is deferred one interval at a time up
to a hard deadline, and an unreachable Agent Server leaves the run RUNNING
with a transient `status_detail`. After every terminal FAILED (callback,
dispatcher or watchdog) the service checks two auto-disable rules and may set
the automation `enabled=false`, `state=INACTIVE` with a `disabled_reason`.
The same loop purges expired run directories in local mode. None of this has
a route: it is observed through `GET /v1/{id}/runs`, `GET /v1/{id}` and the
run directories.

Source: `openhands/automation/watchdog.py`, `openhands/automation/utils/unhealthy.py`, `openhands/automation/utils/agent_server.py`, `openhands/automation/utils/run.py`, `openhands/automation/utils/timeout.py`, `openhands/automation/dispatcher.py`, `openhands/automation/backends/local.py`, `openhands/automation/config.py`

Routes: none

## Sub-features

- `F14.exit-zero`: a run that exits 0 without calling back is marked COMPLETED by the watchdog after its `timeout_at` (`status_detail=null`, `conversation_id=null`), not when the process exits.
- `F14.exit-code`: a run that exits non-zero without calling back is FAILED with `error_detail` `exit_code=3\nstderr: ...` and `status_detail` `{kind: execution_error, code: "3", source: agent_server, operation: verify_run}`.
- `F14.timeout-kill`: a run still going at the automation's `timeout` is killed by the Agent Server (exit -1) and marked FAILED `Timed out: command timed out or was killed` with `status_detail.kind=timeout`, `code="-1"`.
- `F14.late-callback`: a COMPLETED callback for a run the watchdog timed out flips it to COMPLETED and clears `error_detail` and `status_detail`; a FAILED callback for it is 409.
- `F14.still-running-defer`: when `timeout_at` passes while the bash command is still running, the watchdog moves `timeout_at` forward by one interval and keeps the run RUNNING with `status_detail=null`.
- `F14.hard-deadline`: past `started_at + AUTOMATION_SANDBOX_READY_TIMEOUT + timeout + AUTOMATION_RUN_TIMEOUT_HARD_GRACE` a still-running run is FAILED `Timed out: ...` with `status_detail.verification_outcome=still_running`.
- `F14.verification-transient`: with the Agent Server down, a due run stays RUNNING with `status_detail` `{phase: verification, transient: true, source: agent_server, operation: bash_events_search}`.
- `F14.auto-disable-permanent`: three consecutive FAILED runs with a permanent (`auth`) classification disable the automation: `enabled=false`, `state=INACTIVE`, `disabled_reason` `Paused automatically: auth — ...`, `disabled_detail.source=consecutive_permanent_failures`, `disabled_at`, and one disable-event row.
- `F14.re-enable`: `PATCH {"enabled": true}` clears `disabled_reason`, `disabled_detail` and `disabled_at`, and the next permanent failure does not disable it again (history before the disable is ignored).
- `F14.plain-failures`: FAILED callbacks with a plain string error never trip the permanent rule while `AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD` is unset.
- `F14.failure-threshold-env`: `AUTOMATION_FAILURE_DISABLE_THRESHOLD` sets how many permanent failures it takes; 0 turns the permanent rule off.
- `F14.auto-disable-consecutive`: with `AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD=2`, two watchdog-authored FAILED runs with no success in the window disable the automation with `disabled_detail.source=consecutive_failures`.
- `F14.workspace-purge`: with `AUTOMATION_WORKSPACE_RETENTION_SECONDS=1` the watchdog deletes the directories of terminal runs and of stale orphan UUID directories, and keeps directories of RUNNING runs and non-UUID names.

## How to get to it (agent POV)

- No route of its own. The outcome shows up in `GET /v1/{id}/runs`
  (`status`, `error_detail`, `status_detail`, `timeout_at`) and
  `GET /v1/{id}` (`enabled`, `state`, `disabled_reason`, `disabled_detail`,
  `disabled_at`); Agent Canvas shows `disabled_reason` as the "paused" banner
  and re-enables through `PATCH /v1/{id}`. Disable events have no API; the
  recipes read the `automation_disable_events` table with `state sql`.
- The deadline: when the dispatcher marks a run RUNNING it sets
  `timeout_at = now + AUTOMATION_SANDBOX_READY_TIMEOUT + timeout + AUTOMATION_RUN_TIMEOUT_MARGIN`;
  once bash has started it resets it to `now + timeout + AUTOMATION_RUN_TIMEOUT_MARGIN`.
  The Agent Server enforces `timeout` itself (the bash command is started
  with it).
- Config (all read at service start, so recipes use
  `restart --service automation --env ...`):
  `AUTOMATION_WATCHDOG_INTERVAL_SECONDS`, `AUTOMATION_RUN_TIMEOUT_MARGIN`
  (120), `AUTOMATION_RUN_TIMEOUT_HARD_GRACE` (600),
  `AUTOMATION_SANDBOX_READY_TIMEOUT` (300),
  `AUTOMATION_FAILURE_DISABLE_THRESHOLD` (3),
  `AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD` (unset),
  `AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_WINDOW_HOURS` (24),
  `AUTOMATION_WORKSPACE_RETENTION_SECONDS` (604800; 0 turns purging off).
  The automation's own `timeout` (seconds) comes from the create body
  (`fixture automation --timeout`).

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM: the stdlib
  fixtures `echo` (exit 0, no callback), `fail` (exit 3, no callback),
  `sleep` (never ends) and `fail-callback` (FAILED callback with a string).
- The block below restarts the service with `AUTOMATION_RUN_TIMEOUT_MARGIN=1`
  so a run with `timeout=5` is due six seconds after bash starts. Bullets
  that need other settings add them with `restart --env` and end with the
  same reset, so every bullet starts from this baseline.

```sh
control-automation restart --service automation --reset-env --env AUTOMATION_RUN_TIMEOUT_MARGIN=1
```

- **Exit 0 without a callback (`F14.exit-zero`).** The `echo` fixture exits 0 and never calls back; the watchdog completes it once its deadline passes.
  ```sh
  ZA=$(control-automation fixture automation --kind echo --name 'qa watchdog exit0' --timeout 5 --field id)
  T0=$SECONDS
  ZR=$(control-automation api POST "/v1/$ZA/dispatch" --expect 201 --field id)
  control-automation run wait "$ZA" --run-id "$ZR" --timeout 60 --status COMPLETED --check status_detail eq null --check error_detail eq null --check conversation_id eq null --check current_phase eq 'Starting automation' --check completed_at exists --save F14.exit-zero/run
  test $((SECONDS - T0)) -ge 6
  test $((SECONDS - T0)) -le 45
  control-automation api --agent-server GET /api/bash/bash_events/search --query kind__eq=BashOutput --query command_id__eq="$(control-automation api GET "/v1/$ZA/runs" --field runs.0.bash_command_id)" --expect 200 --check items.0.exit_code eq 0 --quiet
  ```
  The run ends at least `timeout + margin` (6 s) after dispatch, though the
  script exits at once: only the watchdog finished it. The Agent Server
  holds exit code 0 for the run's bash command.
- **Non-zero exit without a callback (`F14.exit-code`).** The `fail` fixture prints to stderr and exits 3.
  ```sh
  XA=$(control-automation fixture automation --kind fail --name 'qa watchdog exit3' --timeout 5 --field id)
  XR=$(control-automation api POST "/v1/$XA/dispatch" --expect 201 --field id)
  control-automation run wait "$XA" --run-id "$XR" --timeout 60 --status FAILED --check error_detail matches '^exit_code=3' --check error_detail contains 'stderr: qa failing on purpose' --check status_detail.kind eq execution_error --check status_detail.code eq 3 --check status_detail.source eq agent_server --check status_detail.operation eq verify_run --check status_detail.phase eq execution --save F14.exit-code/run
  ```
- **Killed at the timeout (`F14.timeout-kill`).** The `sleep` fixture never ends; the Agent Server kills it at `timeout=5`.
  ```sh
  KA=$(control-automation fixture automation --kind sleep --name 'qa watchdog kill' --timeout 5 --field id)
  KR=$(control-automation api POST "/v1/$KA/dispatch" --expect 201 --field id)
  control-automation run wait "$KA" --run-id "$KR" --timeout 60 --status FAILED --check error_detail matches '^Timed out: command timed out or was killed' --check status_detail.kind eq timeout --check status_detail.code eq -1 --check status_detail.source eq agent_server --check status_detail.transient eq false --save F14.timeout-kill/run
  KB=$(control-automation api GET "/v1/$KA/runs" --field runs.0.bash_command_id)
  control-automation api --agent-server GET /api/bash/bash_events/search --query kind__eq=BashOutput --query command_id__eq="$KB" --expect 200 --check items.0.exit_code eq -1 --quiet
  ```
- **Late callback reconciles a timeout (`F14.late-callback`).** The timed-out run from the previous bullet gets a callback after the fact.
  Requires: `F14.timeout-kill`
  ```sh
  control-automation api POST "/v1/runs/$KR/complete" --json '{"status":"FAILED","error":"qa late failure"}' --expect 409 --check detail eq 'Run is FAILED, expected RUNNING'
  control-automation api GET "/v1/$KA/runs" --check runs.0.error_detail matches '^Timed out: ' --quiet
  control-automation api POST "/v1/runs/$KR/complete" --json '{"status":"COMPLETED","cost":0.1}' --expect 200 --check status eq COMPLETED --check error_detail eq null --check status_detail eq null --check cost eq 0.1 --save F14.late-callback/complete
  control-automation api GET "/v1/$KA/runs" --check runs.0.status eq COMPLETED --check runs.0.error_detail eq null --check status_counts.COMPLETED eq 1 --check status_counts.FAILED missing --quiet
  ```
  Only a COMPLETED callback over a watchdog `Timed out: ` failure is
  accepted; a late FAILED one is refused and changes nothing.
- **Still running at the deadline (`F14.still-running-defer`).** A negative margin puts `timeout_at` 25 s before the Agent Server's own kill, so the watchdog finds the command alive and defers.
  ```sh
  control-automation restart --service automation --env AUTOMATION_RUN_TIMEOUT_MARGIN=-25
  DA=$(control-automation fixture automation --kind sleep --name 'qa watchdog defer' --timeout 30 --field id)
  DR=$(control-automation api POST "/v1/$DA/dispatch" --expect 201 --field id)
  DT0=$(control-automation api GET "/v1/$DA/runs" --until-ok 60 --check runs.0.current_phase eq 'qa: sleeping' --field runs.0.timeout_at)
  control-automation api GET "/v1/$DA/runs" --until-ok 30 --check runs.0.status eq RUNNING --check runs.0.timeout_at ne "$DT0" --check runs.0.status_detail eq null --save F14.still-running-defer/deferred --quiet
  control-automation run wait "$DA" --run-id "$DR" --timeout 90 --status FAILED --check error_detail matches '^Timed out: command timed out or was killed'
  control-automation restart --service automation --reset-env --env AUTOMATION_RUN_TIMEOUT_MARGIN=1
  ```
  The run's first deadline is 5 s after bash starts; a later read shows it
  still RUNNING with a newer `timeout_at`. It ends only when the Agent
  Server kills it at 30 s.
- **Hard deadline (`F14.hard-deadline`).** Shrink the ready timeout and the hard grace so the hard deadline (`started_at + 0 + 60 - 45`) falls well before the 60 s kill.
  ```sh
  control-automation restart --service automation --env AUTOMATION_RUN_TIMEOUT_MARGIN=-55 --env AUTOMATION_SANDBOX_READY_TIMEOUT=0 --env AUTOMATION_RUN_TIMEOUT_HARD_GRACE=-45
  HA=$(control-automation fixture automation --kind sleep --name 'qa watchdog hard' --timeout 60 --field id)
  T0=$SECONDS
  HR=$(control-automation api POST "/v1/$HA/dispatch" --expect 201 --field id)
  control-automation run wait "$HA" --run-id "$HR" --timeout 90 --status FAILED --check error_detail matches '^Timed out: (Command still running|No bash output found)' --check status_detail.verification_outcome eq still_running --check status_detail.kind eq timeout --check status_detail.phase eq verification --save F14.hard-deadline/run
  test $((SECONDS - T0)) -ge 15
  test $((SECONDS - T0)) -lt 50
  control-automation restart --service automation --reset-env --env AUTOMATION_RUN_TIMEOUT_MARGIN=1
  ```
  The run is FAILED after the 15 s hard deadline and before the 60 s kill
  could have happened. The detail
  reads `Command still running` or `No bash output found`, depending on
  whether the command has written any output yet.
- **Agent Server down during verification (`F14.verification-transient`).** Stop the Agent Server while a run is due; the run stays RUNNING with a transient detail.
  ```sh
  control-automation restart --service automation --env AUTOMATION_RUN_TIMEOUT_MARGIN=-55
  OA=$(control-automation fixture automation --kind sleep --name 'qa watchdog outage' --timeout 60 --field id)
  OR=$(control-automation api POST "/v1/$OA/dispatch" --expect 201 --field id)
  control-automation api GET "/v1/$OA/runs" --until-ok 60 --check runs.0.current_phase eq 'qa: sleeping' --quiet
  control-automation service stop agent-server
  control-automation api GET "/v1/$OA/runs" --until-ok 60 --check runs.0.status eq RUNNING --check runs.0.status_detail.transient eq true --check runs.0.status_detail.phase eq verification --check runs.0.status_detail.source eq agent_server --check runs.0.status_detail.operation eq bash_events_search --save F14.verification-transient/run --quiet
  control-automation service start agent-server
  control-automation api POST "/v1/runs/$OR/cancel" --expect 200
  control-automation restart --service automation --reset-env --env AUTOMATION_RUN_TIMEOUT_MARGIN=1
  ```
  A connection error is never terminal. The run is cancelled afterwards: its
  bash chain died with the Agent Server, so it would otherwise wait for the
  hard deadline.
- **Auto-disable after permanent failures (`F14.auto-disable-permanent`).** Three FAILED callbacks classified `auth` (sent directly to sleeping runs).
  ```sh
  PA=$(control-automation fixture automation --kind sleep --name 'qa auth failing' --timeout 30 --field id)
  for N in 1 2 3; do
    control-automation api GET "/v1/$PA" --check enabled eq true --check disabled_reason eq null --quiet
    PR=$(control-automation api POST "/v1/$PA/dispatch" --expect 201 --field id)
    control-automation run wait "$PA" --run-id "$PR" --status RUNNING --timeout 60
    control-automation api POST "/v1/runs/$PR/complete" --json '{"status":"FAILED","error":{"source":"environment","code":"QA_AUTH","detail":"qa bad key","classification":{"kind":"auth","retryable":false,"user_action":"settings"}}}' --expect 200 --quiet
  done
  control-automation api GET "/v1/$PA" --expect 200 --check enabled eq false --check state eq INACTIVE --check disabled_reason matches '^Paused automatically: auth — QA_AUTH: qa bad key' --check disabled_reason contains 'This failed the last 3 runs' --check disabled_detail.source eq consecutive_permanent_failures --check disabled_detail.threshold eq 3 --check disabled_detail.run_id eq "$PR" --check disabled_at exists --save F14.auto-disable-permanent/automation
  control-automation state sql "SELECT source FROM automation_disable_events WHERE automation_id = '${PA//-/}'" --check count eq 1 --check rows.0.source eq consecutive_permanent_failures
  ```
  The automation is still enabled after the first two failures and paused
  by the third. SQLite stores UUIDs without dashes.
- **Re-enable resets the history (`F14.re-enable`).** Turn the paused automation back on and fail it once more.
  Requires: `F14.auto-disable-permanent`
  ```sh
  control-automation api PATCH "/v1/$PA" --json '{"enabled":true}' --expect 200 --check enabled eq true --check state eq ACTIVE --check disabled_reason eq null --check disabled_detail eq null --check disabled_at eq null --save F14.re-enable/patch
  PR4=$(control-automation api POST "/v1/$PA/dispatch" --expect 201 --field id)
  control-automation run wait "$PA" --run-id "$PR4" --status RUNNING --timeout 60
  control-automation api POST "/v1/runs/$PR4/complete" --json '{"status":"FAILED","error":{"source":"environment","code":"QA_AUTH","detail":"qa bad key","classification":{"kind":"auth","retryable":false,"user_action":"settings"}}}' --expect 200 --quiet
  control-automation api GET "/v1/$PA" --expect 200 --check enabled eq true --check state eq ACTIVE --check disabled_reason eq null
  control-automation api GET "/v1/$PA/runs" --check status_counts.FAILED eq 4 --quiet
  ```
  Four permanent failures in a row, yet still enabled: runs created before
  the latest disable event no longer count.
- **Plain failures do not trip the permanent rule (`F14.plain-failures`).** Three `fail-callback` runs (string error, `kind=execution_error`).
  ```sh
  GA=$(control-automation fixture automation --kind fail-callback --name 'qa plain failing' --field id)
  for N in 1 2 3; do
    GR=$(control-automation api POST "/v1/$GA/dispatch" --expect 201 --field id)
    control-automation run wait "$GA" --run-id "$GR" --status FAILED --timeout 60 --check status_detail.kind eq execution_error
  done
  control-automation api GET "/v1/$GA" --expect 200 --check enabled eq true --check state eq ACTIVE --check disabled_reason eq null --save F14.plain-failures/automation
  ```
- **Permanent-failure threshold (`F14.failure-threshold-env`).** With the threshold at 0, three `auth` failures leave the automation enabled; at 2, two failures pause it.
  ```sh
  control-automation restart --service automation --env AUTOMATION_FAILURE_DISABLE_THRESHOLD=0
  YA=$(control-automation fixture automation --kind sleep --name 'qa threshold off' --timeout 30 --field id)
  for N in 1 2 3; do
    YR=$(control-automation api POST "/v1/$YA/dispatch" --expect 201 --field id)
    control-automation run wait "$YA" --run-id "$YR" --status RUNNING --timeout 60
    control-automation api POST "/v1/runs/$YR/complete" --json '{"status":"FAILED","error":{"source":"environment","code":"QA_AUTH","detail":"qa bad key","classification":{"kind":"auth","retryable":false,"user_action":"settings"}}}' --expect 200 --quiet
  done
  control-automation api GET "/v1/$YA" --expect 200 --check enabled eq true --check disabled_reason eq null --save F14.failure-threshold-env/off
  control-automation restart --service automation --env AUTOMATION_FAILURE_DISABLE_THRESHOLD=2
  YB=$(control-automation fixture automation --kind sleep --name 'qa threshold two' --timeout 30 --field id)
  for N in 1 2; do
    YR=$(control-automation api POST "/v1/$YB/dispatch" --expect 201 --field id)
    control-automation run wait "$YB" --run-id "$YR" --status RUNNING --timeout 60
    control-automation api POST "/v1/runs/$YR/complete" --json '{"status":"FAILED","error":{"source":"environment","code":"QA_AUTH","detail":"qa bad key","classification":{"kind":"auth","retryable":false,"user_action":"settings"}}}' --expect 200 --quiet
  done
  control-automation api GET "/v1/$YB" --expect 200 --check enabled eq false --check disabled_detail.threshold eq 2 --check disabled_reason contains 'This failed the last 2 runs' --save F14.failure-threshold-env/two
  control-automation restart --service automation --reset-env --env AUTOMATION_RUN_TIMEOUT_MARGIN=1
  ```
- **Consecutive-failure rule (`F14.auto-disable-consecutive`).** Off by default; at 2, two watchdog-authored `exit_code=3` failures pause the automation.
  ```sh
  control-automation restart --service automation --env AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD=2
  CA=$(control-automation fixture automation --kind fail --name 'qa consecutive failing' --timeout 5 --field id)
  CR1=$(control-automation api POST "/v1/$CA/dispatch" --expect 201 --field id)
  control-automation run wait "$CA" --run-id "$CR1" --status FAILED --timeout 60 --check error_detail matches '^exit_code=3'
  control-automation api GET "/v1/$CA" --expect 200 --check enabled eq true
  CR2=$(control-automation api POST "/v1/$CA/dispatch" --expect 201 --field id)
  control-automation run wait "$CA" --run-id "$CR2" --status FAILED --timeout 60 --check error_detail matches '^exit_code=3'
  control-automation api GET "/v1/$CA" --until-ok 15 --expect 200 --check enabled eq false --check state eq INACTIVE --check disabled_reason eq 'Paused automatically: the last 2 runs all failed and nothing has succeeded in 24 hours.' --check disabled_detail.source eq consecutive_failures --check disabled_detail.threshold eq 2 --check disabled_detail.window_hours eq 24 --save F14.auto-disable-consecutive/automation
  control-automation restart --service automation --reset-env --env AUTOMATION_RUN_TIMEOUT_MARGIN=1
  ```
  The watchdog runs the auto-disable check right after it marks a run
  terminal, so `exit_code` failures nobody reported count too.
- **Workspace retention (`F14.workspace-purge`).** One finished run, one live run, a stale orphan UUID directory and a non-UUID directory; then restart with a one-second retention.
  ```sh
  WA=$(control-automation fixture automation --kind callback --name 'qa purge done' --field id)
  WR=$(control-automation api POST "/v1/$WA/dispatch" --expect 201 --field id)
  control-automation run wait "$WA" --run-id "$WR" --status COMPLETED --timeout 60
  WL=$(control-automation fixture automation --kind sleep --name 'qa purge live' --timeout 120 --field id)
  WLR=$(control-automation api POST "/v1/$WL/dispatch" --expect 201 --field id)
  control-automation api GET "/v1/$WL/runs" --until-ok 60 --check runs.0.current_phase eq 'qa: sleeping' --quiet
  WORPHAN=$(python3 -c 'import uuid; print(uuid.uuid4())')
  WROOT="$AUTOMATION_VERIFY_RUN/workspaces/automation-runs"
  mkdir -p "$WROOT/$WORPHAN" "$WROOT/qa-not-a-uuid"
  touch -d '2 hours ago' "$WROOT/$WORPHAN" "$WROOT/qa-not-a-uuid"
  control-automation state ls workspaces/automation-runs --expect "$WR" --expect "$WLR" --expect "$WORPHAN" --expect qa-not-a-uuid
  control-automation restart --service automation --env AUTOMATION_WORKSPACE_RETENTION_SECONDS=1
  control-automation exec --timeout 60 --expect-output purged -- sh -c "until [ ! -e '$WROOT/$WR' ] && [ ! -e '$WROOT/$WORPHAN' ]; do sleep 1; done; echo purged"
  control-automation state ls workspaces/automation-runs --expect "$WLR" --expect qa-not-a-uuid
  control-automation api POST "/v1/runs/$WLR/cancel" --expect 200
  control-automation restart --service automation --reset-env --env AUTOMATION_RUN_TIMEOUT_MARGIN=1
  ```
  The finished run's directory and the orphan go within one watchdog scan;
  the RUNNING run's directory and `qa-not-a-uuid` stay. This bullet runs last
  because it deletes every earlier terminal run's directory.

## Gotchas

- Detection time is `timeout + AUTOMATION_RUN_TIMEOUT_MARGIN` after bash
  starts plus up to one watchdog interval: about 12 minutes with defaults.
  Shrink the margin (it may even be negative) and the automation's
  `timeout` rather than waiting.
- A watchdog COMPLETED (exit 0) has no `conversation_id`, `cost` or
  `run_metadata`: those only arrive with a callback.
- The verifier reads only the run's own `bash_command_id`. The Agent
  Server writes a `BashOutput` with an exit code only when the command ends
  (the `sleep` fixture had none while running), so "still running" and "no
  output yet" are the same outcome.
- After `service stop agent-server` the run's bash chain is gone, but the
  service cannot tell: the run defers until the hard deadline
  (`started_at + 300 + timeout + 600` with defaults). Cancel such runs in
  recipes.
- The permanent rule counts only runs whose `status_detail` is permanent:
  `kind` in `auth`, `config`, `quota`, `blocked`, a non-retryable
  classification of those kinds, `user_action=settings`, or
  `permanent: true`; `transient: true` always wins. Plain string errors,
  exit codes and timeouts count only toward the consecutive rule, which is
  off unless `AUTOMATION_CONSECUTIVE_FAILURE_DISABLE_THRESHOLD` is set.
- Manual runs dispatch even on a paused automation (F07.disabled-manual).
- Disable events have no API; `state sql` on
  `automation_disable_events` is the only view.
- Workspace purging needs the service to see the same
  `AUTOMATION_WORKSPACE_BASE` path as the Agent Server; with a real 7-day
  retention nothing is deleted during a verification run.
- `restart --reset-env` drops every `--env` given at launch or restart;
  that is why each bullet here restores the margin explicitly.

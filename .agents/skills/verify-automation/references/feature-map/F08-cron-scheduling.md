# Cron scheduling

How a cron automation turns into runs with nobody calling the API. The
in-process scheduler loop wakes every `AUTOMATION_SCHEDULER_INTERVAL_SECONDS`,
fetches enabled `ACTIVE` automations that it has not polled in the last 60
seconds, and creates one `PENDING` run with `trigger_source="cron"` for each
whose most recent scheduled fire time (computed in the trigger's IANA
timezone) is later than its last trigger, or than its creation when it has
never fired. There is no catch-up and at most one run per poll window, so
nothing fires more often than once a minute whatever the schedule says. The
dispatcher then runs it like any other run, with `AUTOMATION_TRIGGER_SOURCE=cron`
in its environment. Bad schedules and timezones are refused at create time.

Source: `openhands/automation/scheduler.py`, `openhands/automation/utils/cron.py`, `openhands/automation/utils/run.py`, `openhands/automation/schemas.py`, `openhands/automation/capabilities_router.py`

Routes: none

## Sub-features

- `F08.create-validation`: creating a cron automation refuses an invalid expression (`Invalid cron expression: ...`), an impossible one like `0 0 31 2 *` (`Cron expression cannot produce any future fire times: ...`), an unknown timezone (`Invalid timezone: ...`) and a trigger without `type`, each with 422; an omitted timezone is stored as `UTC`.
- `F08.every-minute`: a `* * * * *` automation gets a run with `trigger_source=cron` without any dispatch call, which then completes, and the automation's `last_triggered_at` is set.
- `F08.cron-env`: a cron run's environment carries `AUTOMATION_TRIGGER_SOURCE=cron`, `AUTOMATION_RUN_TRIGGER_SOURCE=cron`, `AUTOMATION_TRIGGER_TYPE=cron` and an `AUTOMATION_EVENT_PAYLOAD` naming the trigger and schedule.
- `F08.timezone`: a daily `M H * * *` schedule in `Asia/Kolkata` fires at that zone's wall-clock minute; the same `M H` in UTC, created at the same time, does not fire.
- `F08.no-catch-up`: a daily schedule whose time already passed today does not fire on creation, although the scheduler has evaluated it (`last_polled_at` set).
- `F08.poll-floor`: an automation fires at most once per 60 s poll window: the first fire comes at least 60 s after creation even when a minute boundary passes sooner, consecutive runs are at least 60 s apart, and a sub-minute six-field schedule (`* * * * * */10`) is accepted at create but still fires once a minute (preflight `POST /v1/validate` flags it `interval_too_short`).
- `F08.disabled`: an automation created `INACTIVE` is never even polled and gets no cron runs; PATCHing a firing automation to `INACTIVE` stops its cron runs while another automation keeps firing.

## How to get to it (agent POV)

- REST, indirectly: create with `POST /v1` (or a preset) and a
  `{"type":"cron","schedule":...,"timezone":...}` trigger, then watch
  `GET /v1/{id}/runs` and `GET /v1/{id}` (`last_triggered_at`). The API
  exposes no next-fire time; the scheduler's own bookkeeping
  (`last_polled_at`) is only in the database (`state sql`).
- `GET /v1/capabilities` reports `triggers.cron.minIntervalSeconds` (the
  floor, `max(60, AUTOMATION_SCHEDULER_INTERVAL_SECONDS)`) and the
  `timezones` list; `POST /v1/validate` and saved drafts (F09) flag faster
  schedules, but `POST /v1` accepts them.
- Inside the run: `AUTOMATION_TRIGGER_SOURCE`, `AUTOMATION_RUN_TRIGGER_SOURCE`,
  `AUTOMATION_TRIGGER_TYPE`, `AUTOMATION_EVENT_PAYLOAD`.
- Config: `AUTOMATION_SCHEDULER_INTERVAL_SECONDS` (launch sets 5 s; the
  60 s per-automation poll floor is hard-coded as `POLL_INTERVAL_SECONDS`).
  `AUTOMATION_SCHEDULER_BATCH_SIZE` exists in config but the loop always uses
  50.
- Agent Canvas: the schedule picker in the automation form, which reads
  `triggers.cron` from capabilities.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`, fast intervals: scheduler every 5 s) and a
  clean `doctor`. No LLM: every automation uses the stdlib `callback`
  fixture, which completes through the callback and dumps its environment to
  `qa-env.json`.
- All scheduled automations are created together, first, so the minute-long
  waits overlap. The Kolkata schedule targets the wall-clock minute that
  starts 15 to 75 seconds from now; the no-catch-up schedule is two minutes
  ago, daily.
  ```sh
  TP=$(control-automation fixture tarball --kind callback --upload --field tarball_path)
  NOW=$(date +%s)
  KSCHED="$(TZ=Asia/Kolkata date -d "@$((NOW + 75))" '+%-M %-H') * * *"
  KFIRE=$(date -u -d "@$((NOW + 75))" '+%Y-%m-%d %H:%M:00')
  NSCHED="$(date -u -d "@$((NOW - 120))" '+%-M %-H') * * *"
  AID=$(control-automation fixture automation --kind callback --name 'qa cron every minute' --cron '* * * * *' --field id)
  SID=$(control-automation fixture automation --kind callback --name 'qa cron every 10s' --cron '* * * * * */10' --field id)
  KID=$(control-automation fixture automation --kind callback --name 'qa cron kolkata' --cron "$KSCHED" --timezone Asia/Kolkata --field id)
  KUTC=$(control-automation fixture automation --kind callback --name 'qa cron kolkata twin utc' --cron "$KSCHED" --field id)
  NID=$(control-automation fixture automation --kind callback --name 'qa cron passed today' --cron "$NSCHED" --field id)
  DID=$(control-automation fixture automation --kind callback --name 'qa cron inactive' --cron '* * * * *' --extra-json '{"state":"INACTIVE"}' --field id)
  AIDX=$(echo "$AID" | tr -d -)
  SIDX=$(echo "$SID" | tr -d -)
  KUTCX=$(echo "$KUTC" | tr -d -)
  NIDX=$(echo "$NID" | tr -d -)
  DIDX=$(echo "$DID" | tr -d -)
  ```
  The `...X` variables hold the dash-less ids SQLite stores.

- **Create-time validation (`F08.create-validation`).** Each refused trigger is a 422 naming the field; the fixtures above stored `UTC` by default.
  ```sh
  control-automation api POST /v1 --json "{\"name\":\"qa bad cron\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"not a cron\"}}" --expect 422 --check detail.0.loc contains schedule --check detail.0.msg eq 'Value error, Invalid cron expression: not a cron' --save F08.create-validation/invalid
  control-automation api POST /v1 --json "{\"name\":\"qa bad cron\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 31 2 *\"}}" --expect 422 --check detail.0.msg eq 'Value error, Cron expression cannot produce any future fire times: 0 0 31 2 *' --save F08.create-validation/impossible
  control-automation api POST /v1 --json "{\"name\":\"qa bad cron\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 9 * * *\",\"timezone\":\"Mars/Olympus\"}}" --expect 422 --check detail.0.loc contains timezone --check detail.0.msg eq 'Value error, Invalid timezone: Mars/Olympus' --save F08.create-validation/timezone
  control-automation api POST /v1 --json "{\"name\":\"qa bad cron\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"trigger\":{\"schedule\":\"* * * * *\"}}" --expect 422 --check detail.0.type eq union_tag_invalid --check detail.0.msg contains __missing_trigger_type__
  control-automation api GET /v1 --query limit=100 --expect 200 --quiet --check automations not-contains 'qa bad cron'
  control-automation api GET "/v1/$AID" --expect 200 --check trigger.schedule eq '* * * * *' --check trigger.timezone eq UTC --check state eq ACTIVE --check last_triggered_at eq null
  control-automation api GET "/v1/$KID" --expect 200 --check trigger.timezone eq Asia/Kolkata --check trigger.schedule eq "$KSCHED"
  ```
- **Fires every minute (`F08.every-minute`).** Nobody dispatches; the scheduler creates the run.
  ```sh
  RID=$(control-automation run wait "$AID" --status PENDING,RUNNING,COMPLETED --timeout 150 --check trigger_source eq cron --field id)
  control-automation run wait "$AID" --run-id "$RID" --status COMPLETED --timeout 60 --check trigger_source eq cron --save F08.every-minute/run
  control-automation api GET "/v1/$AID" --expect 200 --check last_triggered_at ne null --save F08.every-minute/automation
  ```
  The first run appears 60 to 70 seconds after creation (see
  `F08.poll-floor`), not at the next minute boundary.
- **Cron run environment (`F08.cron-env`).**
  Requires: `F08.every-minute`
  ```sh
  control-automation state cat "workspaces/automation-runs/$RID/qa-env.json" --check AUTOMATION_TRIGGER_SOURCE eq cron --check AUTOMATION_RUN_TRIGGER_SOURCE eq cron --check AUTOMATION_TRIGGER_TYPE eq cron --check AUTOMATION_ID eq "$AID" --check AUTOMATION_RUN_ID eq "$RID" --check AUTOMATION_EVENT_PAYLOAD contains '"trigger": "cron"' --check AUTOMATION_EVENT_PAYLOAD contains '"schedule": "* * * * *"'
  ```
- **Timezone (`F08.timezone`).** The Kolkata automation fires at its wall-clock minute (in UTC, `$KFIRE` or just after); its UTC twin with the same `M H` was evaluated but did not fire.
  ```sh
  KRID=$(control-automation run wait "$KID" --status PENDING,RUNNING,COMPLETED --timeout 150 --check trigger_source eq cron --save F08.timezone/run --field id)
  control-automation state sql "SELECT created_at >= '$KFIRE' AS on_time, created_at FROM automation_runs WHERE id = '$(echo "$KRID" | tr -d -)'" --check rows.0.on_time eq 1
  control-automation api GET "/v1/$KUTC/runs" --expect 200 --check total eq 0 --save F08.timezone/utc-twin
  control-automation state sql "SELECT last_polled_at FROM automations WHERE id = '$KUTCX'" --check rows.0.last_polled_at ne null
  ```
- **No catch-up (`F08.no-catch-up`).** By now the scheduler has polled the automation whose daily time passed two minutes before it was created; it has no runs.
  Requires: `F08.every-minute`
  ```sh
  control-automation state sql "SELECT last_polled_at, last_triggered_at FROM automations WHERE id = '$NIDX'" --check rows.0.last_polled_at ne null --check rows.0.last_triggered_at eq null
  control-automation api GET "/v1/$NID/runs" --expect 200 --check total eq 0 --save F08.no-catch-up/runs
  control-automation api GET "/v1/$NID" --expect 200 --check state eq ACTIVE --check last_triggered_at eq null
  ```
  `Requires:` orders it after a fire, which guarantees at least one poll since
  creation.
- **At most one run per 60 s window (`F08.poll-floor`).** Wait for two runs of the every-minute and the every-10-seconds automations, then measure.
  ```sh
  control-automation api GET "/v1/$AID/runs" --until-ok 150 --expect 200 --check total ge 2 --quiet
  control-automation api GET "/v1/$SID/runs" --until-ok 150 --expect 200 --check total ge 2 --check runs.0.trigger_source eq cron --quiet --save F08.poll-floor/sub-minute-runs
  control-automation state sql "SELECT (julianday(min(r.created_at)) - julianday(a.created_at)) * 86400 AS first_after FROM automation_runs r JOIN automations a ON a.id = r.automation_id WHERE a.id = '$AIDX'" --check rows.0.first_after ge 59
  control-automation state sql "SELECT min((julianday(created_at) - julianday(prev)) * 86400) AS gap FROM (SELECT created_at, LAG(created_at) OVER (ORDER BY created_at) AS prev FROM automation_runs WHERE automation_id = '$AIDX') WHERE prev IS NOT NULL" --check rows.0.gap ge 59
  control-automation state sql "SELECT min((julianday(created_at) - julianday(prev)) * 86400) AS gap FROM (SELECT created_at, LAG(created_at) OVER (ORDER BY created_at) AS prev FROM automation_runs WHERE automation_id = '$SIDX') WHERE prev IS NOT NULL" --check rows.0.gap ge 59
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"cron","schedule":"* * * * * */10"}}}' --expect 200 --check valid eq false --check errors.0.code eq interval_too_short --save F08.poll-floor/validate
  ```
  Timestamps are stored to the second, so a 60 s gap can read as 59.99;
  hence `ge 59`.
- **Disabled automations do not fire (`F08.disabled`).** The automation created `INACTIVE` was never fetched; then pause the every-minute one and watch the every-10-seconds one fire twice more while it stays silent.
  Requires: `F08.every-minute`
  ```sh
  control-automation api GET "/v1/$DID/runs" --expect 200 --check total eq 0
  control-automation state sql "SELECT enabled, state, last_polled_at FROM automations WHERE id = '$DIDX'" --check rows.0.state eq INACTIVE --check rows.0.last_polled_at eq null
  control-automation api PATCH "/v1/$AID" --json '{"state":"INACTIVE"}' --expect 200 --check state eq INACTIVE --check enabled eq false
  NA=$(control-automation api GET "/v1/$AID/runs" --expect 200 --field total)
  NS=$(control-automation api GET "/v1/$SID/runs" --expect 200 --field total)
  control-automation api GET "/v1/$SID/runs" --until-ok 200 --expect 200 --check total ge "$((NS + 2))" --quiet
  control-automation api GET "/v1/$AID/runs" --expect 200 --check total eq "$NA" --save F08.disabled/paused-runs
  control-automation api GET "/v1/$AID" --expect 200 --check state eq INACTIVE --check disabled_reason eq manual
  ```
  Two more fires of the control automation span at least 60 s, a full poll
  window in which the paused one would have been due.

## Gotchas

- The first fire is not at the next minute boundary. The first scheduler
  pass after creation stamps `last_polled_at`, and the automation is not
  fetched again for 60 s, so a `* * * * *` automation fires 60 to 70 s after
  it is created, and every 60 s after that (drifting a few seconds with the
  5 s loop).
- No catch-up: a schedule is due only when its latest fire time is later than
  `last_triggered_at` (or `created_at`). Downtime longer than one period
  yields one run, not one per missed fire.
- `POST /v1` accepts schedules faster than the floor (`* * * * * */10`); only
  `POST /v1/validate` and drafts report `interval_too_short`. Such an
  automation simply fires once a minute.
- `croniter` treats a sixth field as seconds.
- Timezone fire times use `zoneinfo`; across DST changes a wall-clock time can
  be skipped or repeated. Recipes pick a zone without DST (`Asia/Kolkata`) so
  the computed minute is exact.
- The API exposes no next-fire time. `last_triggered_at` is on the automation;
  `last_polled_at` (scheduler bookkeeping) only in the database.
- An automation whose stored schedule stops evaluating at runtime is silently
  set `enabled=false`, `state=INACTIVE`, with no `disabled_reason` and no
  event. Create-time validation keeps such schedules out, so this cannot be
  driven through the API.
- With `launch --production-intervals` the loop runs every 60 s and each wait
  here can take up to two minutes longer; keep the fast intervals.
- SQLite stores ids without dashes; `state sql` filters need the hex form.

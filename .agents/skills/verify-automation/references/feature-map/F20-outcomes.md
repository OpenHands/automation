# Automation outcome values

Automation runs and spawned conversations can submit compact, typed outcome
facts through a scoped endpoint. Automations may declare `outcome_parsers` to
normalize submitted payloads with JMESPath expressions, or rely on the strict
flat-object fallback for simple string, number and boolean values. Analytics
clients can list raw outcome values or aggregate them by key.

Source: `openhands/automation/outcome_router.py`, `openhands/automation/outcome_schemas.py`, `openhands/automation/outcome_values.py`, `openhands/automation/router.py`, `openhands/automation/schemas.py`

Routes: `POST /api/automation/v1/outcomes`,
`GET /api/automation/v1/{automation_id}/outcomes/values`,
`GET /api/automation/v1/{automation_id}/outcomes/aggregate`

## Sub-features

- `F20.submit-flat`: `POST /v1/outcomes` accepts a strict flat JSON object when authenticated for an automation without parsers; string, finite-number and boolean values are stored as typed rows, and nested, null, non-finite or oversized values answer 422.
- `F20.submit-parsed`: an automation with `outcome_parsers` extracts normalized values from the submitted payload; parser expression errors, missing/null results and type mismatches answer 422 without storing partial rows.
- `F20.run-scope`: a run-scoped outcome token binds submissions to the token's automation and run, and a user-authenticated submit requires `automation_id` plus an optional `run_id` that belongs to that automation.
- `F20.list-values`: `GET /v1/{id}/outcomes/values` lists outcome rows newest first, scoped to the caller's org, with filters for `key`, `run_id`, `since`, `until`, `limit` and `offset`.
- `F20.aggregate-values`: `GET /v1/{id}/outcomes/aggregate` aggregates by key with `count`, numeric `sum`/`avg`/`min`/`max`, boolean `count_true`/`count_false`, and optional `day`, `week` or `month` buckets.

## How to get to it (agent POV)

- REST: automation code reads `AUTOMATION_OUTCOME_URL` and `AUTOMATION_OUTCOME_TOKEN` from its environment and posts `SubmitOutcomeRequest` JSON. Human or spawned-conversation callers can use the same endpoint with normal user auth and an `automation_id` query parameter.
- Config: raw, preset, plugin-spec and git-sync automation definitions may include `outcome_parsers`; parser keys are unique and use only alphanumeric, underscore, dash and dot characters.
- Reads: analytics consumers use the authenticated automation routes to fetch raw rows or aggregate facts for one automation at a time.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`.
- Use the baseline local session key for user-authenticated examples. Token-auth coverage is handled by service tests because the token is minted per dispatched run.

- **Submit flat values (`F20.submit-flat`, `F20.run-scope`).** Create an automation without parsers, submit simple flat values with user auth, and reject a nested fallback value.
  ```sh
  OID=$(control-automation fixture automation --kind echo --field id)
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"comments_posted":2,"reviewed":true,"repository":"OpenHands/automation"}}' --expect 201 --check stored eq 3 --save F20.submit-flat/submit
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"nested":{"not":"flat"}}}' --expect 422 --check detail contains "must be a string, finite number, or boolean" --save F20.submit-flat/nested
  ```
- **Submit parsed values (`F20.submit-parsed`).** Parser configuration normalizes nested payloads and type mismatches are rejected.
  ```sh
  PID=$(control-automation api POST /v1 --json '{"name":"qa-outcome-parsers","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","outcome_parsers":[{"key":"score","expression":"result.score","value_type":"number"},{"key":"passed","expression":"result.passed","value_type":"boolean"}]}' --expect 201 --field id)
  control-automation api POST "/v1/outcomes?automation_id=$PID" --json '{"payload":{"result":{"score":4.5,"passed":true}}}' --expect 201 --check stored eq 2 --save F20.submit-parsed/submit
  control-automation api POST "/v1/outcomes?automation_id=$PID" --json '{"payload":{"result":{"score":"bad","passed":true}}}' --expect 422 --check detail contains "is not a number"
  ```
- **List and aggregate (`F20.list-values`, `F20.aggregate-values`).** Raw rows and aggregates are scoped to the automation.
  Requires: `F20.submit-parsed`
  ```sh
  control-automation api GET "/v1/$PID/outcomes/values" --query key=score --expect 200 --check values len-ge 1 --check values.0.key eq score --save F20.list-values/list
  control-automation api GET "/v1/$PID/outcomes/aggregate" --query key=score --query operation=avg --expect 200 --check key eq score --check operation eq avg --save F20.aggregate-values/avg
  control-automation api GET "/v1/$PID/outcomes/aggregate" --query key=passed --query operation=count_true --expect 200 --check value eq 1
  ```

## Gotchas

- Outcome tokens are intentionally the same scoped token family as KV tokens, so a run gets one secret with an automation and run claim rather than a second auth system.
- The fallback mode stores only scalar flat facts; declare parsers when the submitted payload has nested structure or values that need explicit datetime or JSON typing.

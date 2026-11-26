# Automation outcome values

Outcome facts belong to a factory and persist across automation replacements.
The factory registers the type of each accepted key. User credentials and run
tokens submit flat payloads or explicit typed facts; the credential determines
whether the source is a user or an agent. Analytics reads the factory's history.

Source: `openhands/automation/outcome_router.py`, `openhands/automation/outcome_schemas.py`, `openhands/automation/outcome_values.py`, `openhands/automation/outcome_periods.py`, `openhands/automation/outcome_shapes.py`, `openhands/automation/outcome_aggregation.py`, `openhands/automation/router.py`, `openhands/automation/schemas.py`

Routes: `POST /api/automation/v1/outcomes`,
`GET /api/automation/v1/factories/{factory_id}/outcomes/values`,
`GET /api/automation/v1/factories/{factory_id}/outcomes/aggregate`

## Sub-features

- `F20.submit-flat`: strict flat scalar payloads become typed facts; nested, null, non-finite, unregistered or oversized values answer 422.
- `F20.submit-typed`: `values` carries `{key, type, value}` facts, including datetime and JSON, without requiring parsers.
- `F20.run-scope`: run tokens derive `source=agent` and the run claim; user credentials require `automation_id` and record `source=user`. An automation without a factory answers 409.
- `F20.list-values`: factory-scoped newest-first rows, filtered by key, automation, run, source or time. Other organizations cannot read them.
- `F20.aggregate-values`: per-factory count, numeric and boolean aggregates, optionally grouped by day, week or month; the same filters apply.
- `F20.totals`: a key registered with `semantics: total` and a `grain` holds one number per period. Submit it with `period`; a later report for the same automation, dimensions and period replaces the earlier one in every read.
- `F20.increments`: a key with the default `semantics: increment` counts every fact. `effective_at` says when it counts (default now), and `dedupe_key` makes a repeated submission count once (`duplicates` in the answer).
- `F20.shapes`: a `json` key can declare `shape: "table.v1"` with `columns`. Every write to it is checked against the columns, and the key routes return the contract.

## How to get to it (agent POV)

- Create a factory with `outcome_keys` and attach an automation before submitting.
- POST a flat `{"payload":{"score":1}}` or typed `{"values":[{"key":"score","type":"number","value":1}]}`. Use a run token or normal user auth with `automation_id`.
- Keys starting with `platform.` (any case) are reserved for metrics the platform computes. Registering one, putting one in `outcome_keys`, or submitting one answers 422; an invalid key on `PUT` or `DELETE /outcome-keys/{key}` answers 422 too.
- Read the authenticated factory routes to fetch raw or aggregate facts across automations. The stored factory id is a snapshot, even if the automation later moves.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`.
- Use the baseline local session key for user-authenticated examples. Token-auth coverage is handled by service tests because the token is minted per dispatched run.

- **Submit flat and typed (`F20.submit-flat`, `F20.submit-typed`, `F20.run-scope`).** Register keys before creating the automation.
  ```sh
  FID=$(control-automation api POST /v1/factories --json '{"name":"QA Outcomes","outcome_keys":{"score":{"type":"number"},"passed":{"type":"boolean"},"metadata":{"type":"json"}}}' --expect 201 --field id)
  OID=$(control-automation api POST /v1 --json "{\"name\":\"qa-outcomes\",\"factory_id\":\"$FID\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"https://qa.invalid/x.tgz\",\"entrypoint\":\"bash run.sh\"}" --expect 201 --field id)
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"score":2,"passed":true}}' --expect 201 --check stored eq 2 --save F20.submit-flat/submit
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"values":[{"key":"metadata","type":"json","value":{"review":"complete"}}]}' --expect 201 --check stored eq 1 --save F20.submit-typed/submit
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"score":{"nested":2}}}' --expect 422
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"unknown":1}}' --expect 422
  NID=$(control-automation fixture automation --kind echo --field id)
  control-automation api POST "/v1/outcomes?automation_id=$NID" --json '{"payload":{"score":1}}' --expect 409 --save F20.run-scope/no-factory
  ```
- **List and aggregate (`F20.list-values`, `F20.aggregate-values`).** Scope reads by factory, not automation.
  Requires: `F20.submit-flat`
  ```sh
  control-automation api GET "/v1/factories/$FID/outcomes/values" --query key=score --query source=user --expect 200 --check total eq 1 --check values.0.key eq score --save F20.list-values/list
  control-automation api GET "/v1/factories/$FID/outcomes/aggregate" --query key=score --query operation=sum --expect 200 --check value eq 2 --save F20.aggregate-values/sum
  control-automation api GET "/v1/factories/$FID/outcomes/aggregate" --query key=passed --query operation=count_true --expect 200 --check value eq 1
  ```
- **Totals and increments (`F20.totals`, `F20.increments`).** A total keeps the latest report for a period. An increment counts once per `dedupe_key`.
  Requires: `F20.submit-flat`
  ```sh
  control-automation api PUT "/v1/factories/$FID/outcome-keys/merged" --json '{"type":"number","semantics":"total","grain":"week"}' --expect 200
  control-automation api PUT "/v1/factories/$FID/outcome-keys/lines" --json '{"type":"number"}' --expect 200
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"merged":3},"period":"2025-W48"}' --expect 201 --save F20.totals/first
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"merged":7},"period":"2025-W48"}' --expect 201 --save F20.totals/replace
  control-automation api GET "/v1/factories/$FID/outcomes/aggregate" --query key=merged --query operation=sum --expect 200 --check value eq 7 --check count eq 1 --save F20.totals/sum
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"merged":1}}' --expect 422
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"lines":4},"effective_at":"2025-11-20T10:00:00Z","dedupe_key":"pr-1"}' --expect 201 --check stored eq 1 --save F20.increments/first
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"payload":{"lines":4},"effective_at":"2025-11-20T10:00:00Z","dedupe_key":"pr-1"}' --expect 201 --check duplicates eq 1 --save F20.increments/repeat
  ```
- **Table shape (`F20.shapes`).** A write that breaks the declared columns answers 422 and names the cell.
  Requires: `F20.submit-typed`
  ```sh
  control-automation api PUT "/v1/factories/$FID/outcome-keys/open_prs" --json '{"type":"json","shape":"table.v1","columns":[{"name":"pr","type":"string"},{"name":"status","type":"string","constraints":{"required":true,"enum":["open","merged"]}}]}' --expect 200
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"values":[{"key":"open_prs","type":"json","value":{"rows":[[{"v":"#818","href":"https://github.com/OpenHands/automation/pull/818"},"open"]],"total_rows":140}}]}' --expect 201 --check stored eq 1 --save F20.shapes/first
  control-automation api POST "/v1/outcomes?automation_id=$OID" --json '{"values":[{"key":"open_prs","type":"json","value":{"rows":[["#818","closed"]]}}]}' --expect 422
  control-automation api PUT "/v1/factories/$FID/outcome-keys/open_prs" --json '{"type":"json"}' --expect 409
  ```

## Gotchas

- `since` is inclusive and `until` is exclusive on both read routes, so adjacent windows (`[a, b)` then `[b, c)`) never count a row twice.
- `since`, `until` and the day, week and month buckets use the time a fact counts at (`effective_at`): the start of its period, the `effective_at` the submission sent, or the time it was received. Rows from before `effective_at` existed count when they were received.
- A total needs `period` in the format of its grain: `2026-11-24T14` (hour), `2026-11-24` (day), `2026-W48` (ISO week) or `2026-11` (month), in UTC. Digits must be ASCII 0-9. A wrong format, a period that has not begun, or `effective_at` or `dedupe_key` on a total answer 422. An increment refuses `period`. A time more than five minutes ahead answers 422.
- `PUT /outcome-keys/{key}` with a different type, `semantics` or `grain` answers 409 once the key has facts. A total must be a number and must have a grain.
- The history stays: `GET .../outcomes/values` lists every report, including replaced totals. Aggregates count only the latest report for each automation, dimensions and period.
- Outcome tokens are intentionally the same scoped token family as KV tokens, so a run gets one secret with an automation and run claim rather than a second auth system.
- Typed items use `type`, not the storage column `value_type`. Source is derived from authentication, never the request body. Use typed values for datetime or JSON; parser-based nested payloads are a separate layer.
- A plugin-spec automation takes no `outcome_parsers`: the create request rejects the field (422), `PATCH` rejects it, and git sync refuses a `plugin_spec` directory that carries it. Outcome declarations for plugin-spec automations belong in the plugin manifest. It can still report with typed `values` or a flat `payload`.
- A shape applies only to a `json` key with `increment` semantics. A `table.v1` key needs `columns`, and `columns` need a shape. The shape and columns are fixed once the key has facts (409). A table value is `{"rows": [...], "total_rows"?: int}` with one cell per column. A cell is a plain value or `{"v", "f"?, "href"?}`. An `href` is an `https://` URL or a path starting `/`. The columns follow the Frictionless Table Schema `fields`, and the `{v, f}` cell follows the Google Charts `DataTable` JSON.

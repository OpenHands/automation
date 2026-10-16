# Drafts

Saved, partial automation setups that the Agent Canvas setup UI keeps while a
user is still filling the form, and test-fires before turning on. A draft
targets one creation endpoint (`/v1`, `/v1/preset/prompt`,
`/v1/preset/plugin`). Saving runs two validation stages: a shape check that
rejects bodies the endpoint could never accept (422), and a final check
against the real create model plus deployment checks (model profile, upload,
cron floor, configured webhook source) whose result is stored as
`validation_errors` and `dispatchable` on a 201. Dispatching a dispatchable
draft materializes it into a hidden `state=DRAFT`, `enabled=false` automation
and queues a manual run on it; the normal automation API refuses to activate
or dispatch that artifact.

Source: `openhands/automation/draft_router.py`, `openhands/automation/draft_schemas.py`, `openhands/automation/schemas.py`, `openhands/automation/capabilities_router.py`, `openhands/automation/utils/run.py`, `openhands/automation/utils/templates.py`

Routes: `POST /api/automation/v1/drafts`, `GET /api/automation/v1/drafts`,
`GET /api/automation/v1/drafts/{draft_id}`, `PATCH /api/automation/v1/drafts/{draft_id}`,
`DELETE /api/automation/v1/drafts/{draft_id}`, `POST /api/automation/v1/drafts/{draft_id}/dispatch`

## Sub-features

- `F09.create-prompt`: a `/v1/preset/prompt` draft saves at any completeness: `{}` is a 201 with `dispatchable=false` and one `missing` error each for `name`, `prompt`, `trigger`; a complete body is `dispatchable=true` with `validation_errors=null`.
- `F09.create-raw`: a `/v1` draft over an uploaded tarball is dispatchable; one whose `oh-internal://` upload does not exist saves with code `tarball_path_404`.
- `F09.create-plugin`: a `/v1/preset/plugin` draft with `plugins` is dispatchable; `plugins` and `variants` together is a shape error.
- `F09.shape-errors`: a draft body the endpoint cannot accept (unknown key, unknown trigger type, `agent_profile_id` on `/v1`, a bad `tarball_path` scheme) is 422 `{"detail":{"message":"Draft body does not match endpoint schema","errors":[{field,code,message}]}}` and stores nothing; an unknown `endpoint` is a plain FastAPI 422; no key is 401.
- `F09.final-validation`: a well-shaped but invalid body is still 201 with field-addressed errors: a bad cron on `trigger.schedule`, `state:"DRAFT"` on `state`, a sub-minute schedule as `interval_too_short`.
- `F09.cron-floor`: the `interval_too_short` floor is `max(60, AUTOMATION_SCHEDULER_INTERVAL_SECONDS)`, matching `GET /v1/capabilities` `triggers.cron.minIntervalSeconds`.
- `F09.name`: the draft's `name` is the request `name` when given, else `draft.name`, else null.
- `F09.source-automation`: `source_automation_id` must name a live automation of the org (else 404 `Automation not found`) and is echoed back.
- `F09.list`: `GET /v1/drafts` returns `{drafts,total}` newest `updated_at` first; `limit` is 1..100.
- `F09.get`: `GET /v1/drafts/{id}` reads one draft; an unknown id is 404 `Automation draft not found`, a non-UUID 422.
- `F09.patch`: PATCH `name` alone renames; PATCH `draft` re-normalizes, re-derives the name and re-validates; PATCH `endpoint` re-checks the stored body under the new endpoint (422 and no change when it does not fit).
- `F09.dispatch`: dispatching a dispatchable draft is 201 with a `PENDING` run, `trigger_source=manual`; the draft gains `materialized_automation_id` and `last_test_run_id`, and the materialized automation is `state=DRAFT`, `enabled=false`, listed by `GET /v1`, and its run completes with `AUTOMATION_TRIGGER_SOURCE=manual`.
- `F09.redispatch`: dispatching the same draft again reuses the materialized automation (updated in place) and adds a new run.
- `F09.not-dispatchable`: dispatching a draft with validation errors is 422 `{"detail":{"message":"Draft is not dispatchable","errors":[...]}}` and creates no automation.
- `F09.event-payload`: an event draft for a source with no webhook is `event_source_not_configured`; once `POST /v1/webhooks` registers it, dispatch with `event_payload` is 201 and the run receives the payload; `event_payload` on a cron draft is 422.
- `F09.artifact-guard`: the materialized DRAFT automation cannot be dispatched (`POST /v1/{id}/dispatch`) or activated (`PATCH state/enabled`) through the normal API: 409, or 422 `Draft is not dispatchable` when the draft has become invalid.
- `F09.preset-materialize`: dispatching a prompt or plugin draft generates a fresh `oh-internal://` upload named `draft-prompt-automation-<name>` / `draft-plugin-automation-<name>`, with `setup_script_path=setup.sh` and entrypoint `.venv/bin/python main.py`.
- `F09.delete`: DELETE is 204; the draft and its materialized automation then answer 404, its pending runs (manual included) become `SKIPPED` with `status_detail.detail` `Automation draft deleted by user`, and a second DELETE is 404.
- `F09.template-after-draft-test`: after a draft carrying `template.id` was test-dispatched, creating the real automation from the same template should create a new ACTIVE automation (known bug: it returns 200 with the hidden DRAFT artifact).

## How to get to it (agent POV)

- REST: the six routes above (draft CRUD and dispatch), plus the normal
  automation routes that read or guard the materialized artifact:
  `GET /v1/{id}`, `GET /v1/{id}/runs`, `POST /v1/{id}/dispatch`,
  `PATCH /v1/{id}`, and `GET /v1` (it lists DRAFT artifacts too).
- Agent Canvas: the automation setup form saves drafts while the user types
  and its "Test run" button calls `/v1/drafts/{id}/dispatch`; `GET
  /v1/capabilities` advertises `automationDrafts` in `features`.
- `POST /v1/validate` (capabilities family) runs the same two validation
  stages without saving anything.
- Config: `AUTOMATION_SCHEDULER_INTERVAL_SECONDS` raises the cron floor;
  webhook rows (`POST /v1/webhooks`) or the built-in webhook secret decide
  whether an event draft is dispatchable.
- There is no SDK or TypeScript client in this repository; every recipe uses
  the REST route through the CLI's `api` verb.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed: dispatched
  drafts use the stdlib `callback` tarball, and preset drafts are dispatched
  only while the dispatcher is paused, so no preset run executes.
- A callback tarball uploaded once for the raw drafts:
  ```sh
  TP=$(control-automation fixture tarball --kind callback --upload --field tarball_path)
  test -n "$TP"
  ```

- **Prompt draft at any completeness (`F09.create-prompt`).** An empty body saves with three `missing` errors; a complete body is dispatchable. GET returns the stored, normalized body.
  ```sh
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{}}' --expect 201 --check dispatchable eq false --check name eq null --check validation_errors len-eq 3 --check validation_errors.0.field eq name --check validation_errors.0.code eq missing --check validation_errors.1.field eq prompt --check validation_errors.2.field eq trigger --save F09.create-prompt/empty
  PD=$(control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa draft prompt","prompt":"say hi","trigger":{"type":"cron","schedule":"0 9 * * 1"}}}' --expect 201 --check dispatchable eq true --check validation_errors eq null --check materialized_automation_id eq null --field id)
  control-automation api GET "/v1/drafts/$PD" --expect 200 --check endpoint eq /v1/preset/prompt --check draft.prompt eq 'say hi' --check draft.trigger.schedule eq '0 9 * * 1' --check dispatchable eq true --save F09.create-prompt/readback
  ```
- **Raw draft (`F09.create-raw`).** Over the uploaded tarball it is dispatchable; over an upload id that does not exist it saves with `tarball_path_404`.
  ```sh
  RD=$(control-automation api POST /v1/drafts --json "{\"endpoint\":\"/v1\",\"draft\":{\"name\":\"qa raw draft\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"}}}" --expect 201 --check dispatchable eq true --check validation_errors eq null --save F09.create-raw/ok --field id)
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1","draft":{"name":"qa raw missing","tarball_path":"oh-internal://uploads/00000000-0000-0000-0000-000000000000","entrypoint":"bash run.sh","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 201 --check dispatchable eq false --check validation_errors.0.field eq tarball_path --check validation_errors.0.code eq tarball_path_404 --save F09.create-raw/missing-upload
  control-automation api GET "/v1/drafts/$RD" --expect 200 --check draft.tarball_path eq "$TP" --check draft.entrypoint eq 'bash run.sh'
  ```
- **Plugin draft (`F09.create-plugin`).** `plugins` alone is dispatchable (the plugin is not fetched at save time); with `variants` too the shape stage refuses it.
  ```sh
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/plugin","draft":{"name":"qa plugin draft","prompt":"hi","plugins":[{"source":"github:owner/repo"}],"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 201 --check dispatchable eq true --check draft.plugins.0.source eq github:owner/repo --save F09.create-plugin/ok
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/plugin","draft":{"plugins":[{"source":"github:owner/repo"}],"variants":[{"name":"a"}]}}' --expect 422 --check detail.message eq 'Draft body does not match endpoint schema' --check detail.errors.0.message contains "Only one of 'plugins' or 'variants'"
  ```
- **Shape errors (`F09.shape-errors`).** Each refused body leaves the draft count unchanged; a positive control saves.
  ```sh
  N0=$(control-automation api GET /v1/drafts --expect 200 --field total)
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"foo":1}}' --expect 422 --check detail.message eq 'Draft body does not match endpoint schema' --check detail.errors.0.field eq foo --check detail.errors.0.code eq extra_forbidden --save F09.shape-errors/extra-key
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"trigger":{"type":"bogus"}}}' --expect 422 --check detail.errors.0.field eq trigger --check detail.errors.0.code eq union_tag_invalid
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1","draft":{"agent_profile_id":"qa-profile"}}' --expect 422 --check detail.errors.0.field eq agent_profile_id --check detail.errors.0.code eq extra_forbidden
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1","draft":{"tarball_path":"ftp://example.invalid/x.tar.gz"}}' --expect 422 --check detail.errors.0.field eq tarball_path --check detail.errors.0.message contains 'tarball_path must start with'
  control-automation api POST /v1/drafts --json '{"endpoint":"/v2","draft":{}}' --expect 422 --check detail.0.loc.1 eq endpoint --check detail.0.type eq literal_error
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1","draft":{}}' --auth none --expect 401 --check detail contains 'Authentication required'
  control-automation api GET /v1/drafts --expect 200 --check total eq "$N0"
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1","draft":{}}' --expect 201 --check dispatchable eq false
  control-automation api GET /v1/drafts --expect 200 --check total eq "$((N0 + 1))"
  ```
- **Final-stage errors (`F09.final-validation`).** Well-shaped but invalid bodies are 201 with `dispatchable=false` and one error each.
  ```sh
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa bad cron","prompt":"hi","trigger":{"type":"cron","schedule":"bogus"}}}' --expect 201 --check dispatchable eq false --check validation_errors.0.field eq trigger.schedule --check validation_errors.0.message contains 'Invalid cron expression: bogus' --save F09.final-validation/bad-cron
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa draft state","prompt":"hi","state":"DRAFT","trigger":{"type":"cron","schedule":"0 9 * * 1"}}}' --expect 201 --check dispatchable eq false --check validation_errors.0.field eq state --check validation_errors.0.message contains 'state=DRAFT is reserved'
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa too fast","prompt":"hi","trigger":{"type":"cron","schedule":"* * * * * */10"}}}' --expect 201 --check dispatchable eq false --check validation_errors len-eq 1 --check validation_errors.0.field eq trigger.schedule --check validation_errors.0.code eq interval_too_short --check validation_errors.0.message eq 'This deployment fires an automation at most once every 60 seconds.' --save F09.final-validation/interval
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa every minute","prompt":"hi","trigger":{"type":"cron","schedule":"* * * * *"}}}' --expect 201 --check dispatchable eq true
  ```
  The six-field schedule asks for every 10 seconds; `* * * * *` (exactly 60 s) clears the floor.
- **Cron floor follows the scheduler interval (`F09.cron-floor`).** Restart with a 120 s scheduler interval, then restore.
  ```sh
  control-automation restart --service automation --env AUTOMATION_SCHEDULER_INTERVAL_SECONDS=120
  control-automation api GET /v1/capabilities --expect 200 --quiet --check triggers.cron.minIntervalSeconds eq 120
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa minute at 120","prompt":"hi","trigger":{"type":"cron","schedule":"* * * * *"}}}' --expect 201 --check dispatchable eq false --check validation_errors.0.code eq interval_too_short --check validation_errors.0.message eq 'This deployment fires an automation at most once every 120 seconds.' --save F09.cron-floor/120
  control-automation restart --service automation --reset-env
  control-automation api GET /v1/capabilities --expect 200 --quiet --check triggers.cron.minIntervalSeconds eq 60
  ```
- **Name precedence (`F09.name`).**
  ```sh
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","name":"qa explicit","draft":{"name":"qa from body"}}' --expect 201 --check name eq 'qa explicit' --check draft.name eq 'qa from body'
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa from body"}}' --expect 201 --check name eq 'qa from body' --save F09.name/from-body
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"prompt":"hi"}}' --expect 201 --check name eq null
  ```
- **Source automation (`F09.source-automation`).** An unknown id is 404 and saves nothing; a live automation id is echoed.
  ```sh
  SRC=$(control-automation fixture automation --kind callback --name 'qa draft source' --field id)
  control-automation api POST /v1/drafts --json '{"endpoint":"/v1","draft":{},"source_automation_id":"00000000-0000-0000-0000-000000000000"}' --expect 404 --check detail eq 'Automation not found' --save F09.source-automation/unknown
  SD=$(control-automation api POST /v1/drafts --json "{\"endpoint\":\"/v1\",\"draft\":{},\"source_automation_id\":\"$SRC\"}" --expect 201 --check source_automation_id eq "$SRC" --field id)
  control-automation api GET "/v1/drafts/$SD" --expect 200 --check source_automation_id eq "$SRC"
  ```
- **List (`F09.list`).** Renaming an older draft moves it to the front; `limit` bounds are enforced.
  Requires: `F09.create-prompt`
  ```sh
  control-automation api PATCH "/v1/drafts/$PD" --json '{"name":"qa draft prompt touched"}' --expect 200 --check name eq 'qa draft prompt touched'
  control-automation api GET /v1/drafts --expect 200 --quiet --check drafts.0.id eq "$PD" --check total ge 10 --save F09.list/ordered
  control-automation api GET /v1/drafts --query limit=1 --expect 200 --quiet --check drafts len-eq 1 --check total ge 10
  control-automation api GET /v1/drafts --query limit=1 --query offset=1 --expect 200 --quiet --check drafts len-eq 1 --check drafts.0.id ne "$PD"
  control-automation api GET /v1/drafts --query limit=0 --expect 422
  control-automation api GET /v1/drafts --query limit=101 --expect 422
  ```
- **Get one (`F09.get`).**
  Requires: `F09.create-prompt`
  ```sh
  control-automation api GET "/v1/drafts/$PD" --expect 200 --check id eq "$PD" --check user_id exists --check org_id exists --check created_at exists --check updated_at exists
  control-automation api GET /v1/drafts/00000000-0000-0000-0000-000000000000 --expect 404 --check detail eq 'Automation draft not found' --save F09.get/unknown
  control-automation api GET /v1/drafts/not-a-uuid --expect 422 --check detail.0.loc.1 eq draft_id
  ```
- **Update (`F09.patch`).** Rename, replace the body, and try an endpoint the body does not fit.
  ```sh
  ED=$(control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{}}' --expect 201 --field id)
  control-automation api PATCH "/v1/drafts/$ED" --json '{"name":"qa renamed"}' --expect 200 --check name eq 'qa renamed' --check dispatchable eq false
  control-automation api GET "/v1/drafts/$ED" --expect 200 --check name eq 'qa renamed' --check draft eq '{}'
  control-automation api PATCH "/v1/drafts/$ED" --json '{"draft":{"name":"qa patched body","prompt":"say hi","trigger":{"type":"cron","schedule":"0 9 * * 1"}}}' --expect 200 --check name eq 'qa patched body' --check dispatchable eq true --check validation_errors eq null --save F09.patch/body
  control-automation api PATCH "/v1/drafts/$ED" --json '{"endpoint":"/v1"}' --expect 422 --check detail.message eq 'Draft body does not match endpoint schema' --check detail.errors.0.field eq prompt --save F09.patch/endpoint-mismatch
  control-automation api PATCH "/v1/drafts/$ED" --json '{"bogus":1}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api GET "/v1/drafts/$ED" --expect 200 --check endpoint eq /v1/preset/prompt --check draft.prompt eq 'say hi' --check dispatchable eq true
  control-automation api PATCH /v1/drafts/00000000-0000-0000-0000-000000000000 --json '{"name":"x"}' --expect 404
  ```
  Replacing `draft` without a request `name` re-derives the name from the
  body, which overwrites the earlier rename.
- **Dispatch (`F09.dispatch`).** The raw draft becomes a DRAFT automation with a manual run that completes through the callback.
  Requires: `F09.create-raw`
  ```sh
  R1=$(control-automation api POST "/v1/drafts/$RD/dispatch" --expect 201 --check status eq PENDING --check trigger_source eq manual --save F09.dispatch/run --field id)
  MA=$(control-automation api GET "/v1/drafts/$RD" --expect 200 --check last_test_run_id eq "$R1" --check materialized_automation_id exists --field materialized_automation_id)
  control-automation api GET "/v1/$MA" --expect 200 --check state eq DRAFT --check enabled eq false --check disabled_reason eq null --check name eq 'qa raw draft' --check tarball_path eq "$TP" --save F09.dispatch/automation
  control-automation run wait "$MA" --run-id "$R1" --status COMPLETED --timeout 90 --check trigger_source eq manual
  control-automation state cat "workspaces/automation-runs/$R1/qa-env.json" --check AUTOMATION_TRIGGER_SOURCE eq manual --check AUTOMATION_ID eq "$MA"
  control-automation api GET /v1 --query limit=100 --expect 200 --quiet --check automations contains "$MA"
  ```
- **Dispatch again (`F09.redispatch`).** Same materialized automation, a second run.
  Requires: `F09.dispatch`
  ```sh
  R2=$(control-automation api POST "/v1/drafts/$RD/dispatch" --expect 201 --check automation_id eq "$MA" --field id)
  test "$R2" != "$R1"
  control-automation api GET "/v1/drafts/$RD" --expect 200 --check materialized_automation_id eq "$MA" --check last_test_run_id eq "$R2"
  control-automation api GET "/v1/$MA/runs" --expect 200 --check total eq 2 --save F09.redispatch/runs
  control-automation run wait "$MA" --run-id "$R2" --status COMPLETED --timeout 90
  ```
- **Not dispatchable (`F09.not-dispatchable`).**
  ```sh
  BD=$(control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa incomplete"}}' --expect 201 --check dispatchable eq false --field id)
  control-automation api POST "/v1/drafts/$BD/dispatch" --expect 422 --check detail.message eq 'Draft is not dispatchable' --check detail.errors.0.field eq prompt --save F09.not-dispatchable/refused
  control-automation api GET "/v1/drafts/$BD" --expect 200 --check materialized_automation_id eq null --check last_test_run_id eq null
  control-automation api POST /v1/drafts/00000000-0000-0000-0000-000000000000/dispatch --expect 404 --check detail eq 'Automation draft not found'
  ```
- **Event draft with a test payload (`F09.event-payload`).** Not dispatchable until a webhook delivers its source; then the payload reaches the run. A cron draft refuses a payload.
  Requires: `F09.create-raw`
  ```sh
  EVD=$(control-automation api POST /v1/drafts --json "{\"endpoint\":\"/v1\",\"draft\":{\"name\":\"qa event draft\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"trigger\":{\"type\":\"event\",\"source\":\"qa-drafts\",\"on\":\"order.*\"}}}" --expect 201 --check dispatchable eq false --check validation_errors.0.field eq trigger.source --check validation_errors.0.code eq event_source_not_configured --field id)
  control-automation api POST "/v1/drafts/$EVD/dispatch" --json '{"event_payload":{"type":"order.created"}}' --expect 422 --check detail.message eq 'Draft is not dispatchable'
  control-automation api POST /v1/webhooks --json '{"name":"qa drafts hook","source":"qa-drafts"}' --expect 201 --quiet
  control-automation api GET "/v1/drafts/$EVD" --expect 200 --check dispatchable eq false
  ER=$(control-automation api POST "/v1/drafts/$EVD/dispatch" --json '{"event_payload":{"type":"order.created","qa":"draft-payload"}}' --expect 201 --check trigger_source eq manual --field id)
  EMA=$(control-automation api GET "/v1/drafts/$EVD" --expect 200 --check dispatchable eq true --check validation_errors eq null --field materialized_automation_id)
  control-automation run wait "$EMA" --run-id "$ER" --status COMPLETED --timeout 90
  control-automation state cat "workspaces/automation-runs/$ER/qa-env.json" --check AUTOMATION_TRIGGER_TYPE eq event --check AUTOMATION_TRIGGER_SOURCE eq manual --check AUTOMATION_EVENT_PAYLOAD contains draft-payload
  control-automation api POST "/v1/drafts/$RD/dispatch" --json '{"event_payload":{"type":"x"}}' --expect 422 --check detail eq 'event_payload can only be used with event-triggered drafts' --save F09.event-payload/cron-refused
  ```
  The GET right after the webhook is created still says `dispatchable=false`:
  validation is recomputed only on POST, PATCH and dispatch.
- **Normal API guards the artifact (`F09.artifact-guard`).** 409 while the draft is valid; 422 once the draft is edited into an invalid state.
  Requires: `F09.dispatch`
  ```sh
  control-automation api POST "/v1/$MA/dispatch" --expect 409 --check detail contains 'must be dispatched or activated through the draft API' --save F09.artifact-guard/dispatch
  control-automation api PATCH "/v1/$MA" --json '{"state":"ACTIVE"}' --expect 409
  control-automation api PATCH "/v1/$MA" --json '{"enabled":true}' --expect 409
  control-automation api GET "/v1/$MA" --expect 200 --check state eq DRAFT --check enabled eq false
  control-automation api PATCH "/v1/drafts/$RD" --json "{\"draft\":{\"name\":\"qa raw draft\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\"}}" --expect 200 --check dispatchable eq false
  control-automation api POST "/v1/$MA/dispatch" --expect 422 --check detail.message eq 'Draft is not dispatchable' --check detail.errors.0.field eq trigger --save F09.artifact-guard/invalid-draft
  ```
- **Preset drafts materialize a generated tarball (`F09.preset-materialize`).** With the dispatcher paused, dispatch a prompt and a plugin draft and inspect what was materialized.
  ```sh
  control-automation restart --service automation --env AUTOMATION_DISPATCHER_INTERVAL_SECONDS=3600
  QP=$(control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa prompt mat","prompt":"say hi","trigger":{"type":"cron","schedule":"0 9 * * 1"}}}' --expect 201 --check dispatchable eq true --field id)
  QR=$(control-automation api POST "/v1/drafts/$QP/dispatch" --expect 201 --check status eq PENDING --field id)
  QM=$(control-automation api GET "/v1/drafts/$QP" --expect 200 --field materialized_automation_id)
  QT=$(control-automation api GET "/v1/$QM" --expect 200 --check state eq DRAFT --check setup_script_path eq setup.sh --check entrypoint eq '.venv/bin/python main.py' --check preset_metadata.preset_type eq prompt --check tarball_path matches '^oh-internal://uploads/' --save F09.preset-materialize/prompt --field tarball_path)
  QG=$(control-automation api POST /v1/drafts --json '{"endpoint":"/v1/preset/plugin","draft":{"name":"qa plugin mat","prompt":"hi","plugins":[{"source":"github:owner/repo"}],"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 201 --field id)
  control-automation api POST "/v1/drafts/$QG/dispatch" --expect 201 --check status eq PENDING
  QGM=$(control-automation api GET "/v1/drafts/$QG" --expect 200 --field materialized_automation_id)
  control-automation api GET "/v1/$QGM" --expect 200 --check state eq DRAFT --check preset_metadata.preset_type eq plugin --check entrypoint eq '.venv/bin/python main.py'
  control-automation api GET /v1/uploads --expect 200 --quiet --check uploads contains 'draft-prompt-automation-qa prompt mat' --check uploads contains 'draft-plugin-automation-qa plugin mat' --check uploads contains "$QT"
  control-automation run wait "$QM" --run-id "$QR" --status PENDING --timeout 10
  ```
  The prompt draft's run stays `PENDING` here; the next bullet deletes the
  draft while it waits and then restores the dispatcher.
- **Delete (`F09.delete`).** The pending run of the deleted draft is skipped, the artifact disappears, and the draft count drops.
  Requires: `F09.preset-materialize`
  ```sh
  N1=$(control-automation api GET /v1/drafts --expect 200 --field total)
  control-automation api DELETE "/v1/drafts/$QP" --expect 204 --save F09.delete/delete
  control-automation api GET "/v1/drafts/$QP" --expect 404 --check detail eq 'Automation draft not found'
  control-automation api GET "/v1/$QM" --expect 404 --check detail eq 'Automation not found'
  control-automation api GET /v1/drafts --expect 200 --check total eq "$((N1 - 1))"
  control-automation state sql "SELECT status, error_detail, json_extract(status_detail, '\$.detail') AS reason FROM automation_runs WHERE id = '$(echo "$QR" | tr -d -)'" --check rows.0.status eq SKIPPED --check rows.0.error_detail eq 'Automation disabled' --check rows.0.reason eq 'Automation draft deleted by user'
  control-automation api DELETE "/v1/drafts/$QP" --expect 404
  control-automation api DELETE "/v1/drafts/$QG" --expect 204
  control-automation restart --service automation --reset-env
  ```
  The deleted automation's runs list is gone with it (`GET /v1/{id}/runs` is
  404), so the run row is read from the database.
- **Template enable after a draft test (`F09.template-after-draft-test`), known bug.** The real create should make a new ACTIVE automation; it returns the hidden DRAFT artifact instead.
  Requires: `F09.create-raw`
  ```sh
  TD=$(control-automation api POST /v1/drafts --json "{\"endpoint\":\"/v1\",\"draft\":{\"name\":\"qa tpl draft\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"template\":{\"id\":\"qa-tpl-draft\",\"version\":\"1\"},\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"}}}" --expect 201 --check dispatchable eq true --field id)
  control-automation api POST "/v1/drafts/$TD/dispatch" --expect 201 --quiet
  TMA=$(control-automation api GET "/v1/drafts/$TD" --expect 200 --field materialized_automation_id)
  control-automation api GET "/v1/$TMA" --expect 200 --check state eq DRAFT
  control-automation api POST /v1 --json "{\"name\":\"qa tpl real\",\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\",\"template\":{\"id\":\"qa-tpl-draft\",\"version\":\"1\"},\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"}}" --save F09.template-after-draft-test/create --expect 201 --check state eq ACTIVE --check id ne "$TMA"  # bug
  ```
  Observed: `200` with `id` equal to the DRAFT artifact (`state=DRAFT`,
  `enabled=false`), which the normal API then refuses to activate (409), so
  the user cannot turn the template on without deleting the draft first.
  `find_existing_template_automation` (`utils/templates.py`) filters on
  `deleted_at` and `template.id` but not on `state`. The same happens through
  `POST /v1/preset/prompt` with that template id.

## Gotchas

- Only a shape mismatch is a 4xx. An invalid-but-well-shaped draft is a 201
  with `dispatchable=false`; read `validation_errors`, not the status code.
- The two 422 envelopes differ: the shape stage and "not dispatchable" put an
  object `{message, errors}` in `detail`; FastAPI's own body validation
  (`endpoint`, unknown top-level keys) puts the usual list in `detail`.
- `validation_errors` and `dispatchable` are stored, not live. Creating the
  webhook, uploading the tarball or changing the scheduler interval does not
  change a saved draft until it is PATCHed or dispatched; a GET shows the stale
  verdict.
- `updated_at` (and so the list order) moves only when a PATCH changes a
  stored value; a PATCH that sends the current name back is a no-op.
- Changing `endpoint` keeps the stored body and re-checks it; a prompt body
  under `/v1` fails on `prompt` (extra field). Send `endpoint` and `draft`
  together to switch shapes.
- A materialized draft automation is listed by `GET /v1` (no state filter) and
  its runs by `GET /v1/{id}/runs`, but the scheduler never fires it
  (`enabled=false`, `state=DRAFT`) and the normal API refuses to dispatch or
  activate it.
- Every preset-draft dispatch writes a new `draft-*-automation-*` upload; old
  ones are not cleaned up.
- A dispatched preset draft really runs the preset (`setup.sh` installs the
  SDK from PyPI and the agent needs an LLM). The recipe pauses the dispatcher
  (`AUTOMATION_DISPATCHER_INTERVAL_SECONDS=3600`) so nothing executes; `restart
  --reset-env` restores the fast intervals.
- Deleting a draft skips pending runs with `error_detail` `Automation
  disabled`; the draft-specific reason is only in `status_detail.detail`.
- Creator-only checks (403 `Only the draft creator can modify or dispatch
  it`) and the `manage_automations` permission (403) cannot be reached in
  local mode, where one user holds every permission.
- `agent_profile_id` and `observability_associations` are accepted by `POST
  /v1` but not by a `/v1` draft (shape 422).

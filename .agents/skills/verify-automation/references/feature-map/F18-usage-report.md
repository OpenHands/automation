# Usage totals for the OpenHands server

The one route another service reads instead of a user: the OpenHands server
reports each self-hosted install's usage to Customer Center, and automations
live in this service's database, so it asks `GET /v1/usage` how many there
are across every organization. The caller proves itself with the service key
this service otherwise uses to call the server (`AUTOMATION_SERVICE_KEY`),
sent as `X-Service-API-Key`; no user credential is read. The answer is one
number: live (not soft-deleted) automations, whatever their state.

Source: `openhands/automation/usage_router.py`, `openhands/automation/schemas.py`, `openhands/automation/config.py`

Routes: `GET /api/automation/v1/usage`

## Sub-features

- `F18.unconfigured`: with no `AUTOMATION_SERVICE_KEY` (the local-mode default) the route answers 503 `Service authentication not configured`, whatever the caller sends.
- `F18.service-key`: with a key configured, a request without `X-Service-API-Key` or with a wrong one answers 401 `Invalid service API key`; the local session key, as header or Bearer, does not open it.
- `F18.count`: with the right key the route answers 200 `{"automations": N}`, where N counts the automations that are not soft-deleted, across every org and in any state: creating one raises it by one and deleting that one brings it back.

## How to get to it (agent POV)

- REST: `GET /api/automation/v1/usage` with `X-Service-API-Key: <AUTOMATION_SERVICE_KEY>`,
  answering `{"automations": <int>}`. The session key is ignored, so
  `api --auth none --header 'X-Service-API-Key: ...'` is the natural call.
- Consumer: the OpenHands server's usage-report worker job
  (`server/licensing/usage_report.py`, `automation_count()`) calls it every
  30 minutes with its `AUTOMATIONS_SERVICE_KEY`, the same value as this
  service's `AUTOMATION_SERVICE_KEY`, and sends Customer Center `null` for
  the count whenever the call fails.
- Config: `AUTOMATION_SERVICE_KEY` (empty means 503). `launch` does not set
  it: local mode has no SaaS API to call, so the recipes set it with
  `restart --env` and restore the baseline afterwards.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.
- Bullets run in order: the first expects the baseline (no service key), the
  second configures one that the third relies on, and the third restores the
  baseline at its end.

- **No service key configured (`F18.unconfigured`).** The local-mode default. Every caller gets 503, even one that sends a key.
  ```sh
  control-automation api GET /v1/usage --auth none --expect 503 --check detail eq 'Service authentication not configured' --save F18.unconfigured/no-key
  control-automation api GET /v1/usage --auth none --header 'X-Service-API-Key: qa-service-key' --expect 503 --check detail eq 'Service authentication not configured'
  ```
- **Only the service key gets in (`F18.service-key`).** Configure a key, then call without it, with a wrong one, and with the local key in both of its forms.
  ```sh
  control-automation restart --service automation --env AUTOMATION_SERVICE_KEY=qa-service-key
  control-automation api GET /v1/usage --auth none --expect 401 --check detail eq 'Invalid service API key' --save F18.service-key/missing
  control-automation api GET /v1/usage --auth none --header 'X-Service-API-Key: not-the-key' --expect 401 --check detail eq 'Invalid service API key' --save F18.service-key/wrong
  control-automation api GET /v1/usage --auth key --expect 401 --check detail eq 'Invalid service API key'
  control-automation api GET /v1/usage --auth bearer --expect 401 --check detail eq 'Invalid service API key'
  ```
  Leaves `AUTOMATION_SERVICE_KEY=qa-service-key` set.
- **The count follows live automations (`F18.count`).** Read the total, add an automation, read again, delete it, read again: the soft-deleted row is still in the table but no longer counted. Restores the baseline.
  Requires: `F18.service-key`
  ```sh
  N0=$(control-automation api GET /v1/usage --auth none --header 'X-Service-API-Key: qa-service-key' --expect 200 --check automations matches '^[0-9]+$' --field automations)
  UAID=$(control-automation fixture automation --kind callback --name qa-usage-count --field id)
  control-automation api GET /v1/usage --auth none --header 'X-Service-API-Key: qa-service-key' --expect 200 --check automations eq "$((N0 + 1))" --save F18.count/after-create
  control-automation api DELETE "/v1/$UAID" --expect 204
  control-automation api GET /v1/usage --auth none --header 'X-Service-API-Key: qa-service-key' --expect 200 --check automations eq "$N0" --save F18.count/after-delete
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name = 'qa-usage-count' AND deleted_at IS NOT NULL" --check rows.0.n ge 1
  control-automation restart --service automation --reset-env
  control-automation api GET /v1/usage --auth none --expect 503
  ```

## Gotchas

- Local mode never configures `AUTOMATION_SERVICE_KEY` (the dispatcher only
  needs it in cloud mode), so a fresh run answers 503 until a bullet sets
  it. A self-hosted install runs in cloud mode and always has the key, so
  there the route answers 401 or 200.
- The count is `deleted_at IS NULL` over every org: a disabled or draft-state
  automation counts, a deleted one does not. It is not `GET /v1`'s `total`,
  which is one org's.
- The route is reachable through the OpenHands ingress like every other
  `/api/automation` route; the key is what protects it. Its error bodies use
  `detail`, like the user-auth refusals, not the probes' `error`.
- The OpenHands server turns every failure here into `automations: null` in
  its report (and `Not reported` on Customer Center's admin page): a wrong or
  missing key on either side, an unreachable service, or an older image that
  answers 404. Check the server's `usage_report_automation_count_failed`
  warning, not the report, to tell them apart.

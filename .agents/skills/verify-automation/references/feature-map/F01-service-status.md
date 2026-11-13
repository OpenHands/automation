# Service status, version and API contract

The probes and documents an agent or a sandbox script reads before it calls
anything else: liveness and readiness (with and without the base path), the
SDK version a preset's `setup.sh` installs, the service and SDK versions, the
OpenAPI document, and the base path that `AUTOMATION_BASE_URL` sets for every
other route. None of them needs a key.

Source: `openhands/automation/app.py`, `openhands/automation/utils/version.py`, `openhands/automation/config.py`

Routes: `GET /health`, `GET /api/automation/health`, `GET /ready`,
`GET /api/automation/ready`, `GET /sdk-version`, `GET /api/automation/sdk-version`,
`GET /server_info`, `GET /api/automation/server_info`,
`GET /api/automation/openapi.json`

## Sub-features

- `F01.health`: `GET /health` and `GET /api/automation/health` answer 200 `{"status":"ok"}` without a key.
- `F01.ready`: `GET /ready` and its base-path twin answer 200 `{"status":"ready"}` once the database is reachable.
- `F01.sdk-version`: `GET /sdk-version` (and its base-path twin) answers the installed `openhands-sdk` version without a key; preset runs install exactly this version.
- `F01.server-info`: `GET /server_info` reports `package_version` and an `sdk_version` equal to `/sdk-version`.
- `F01.openapi`: `GET /api/automation/openapi.json` serves the OpenAPI document (title `OpenHands Automations Service`); there is none at `/openapi.json`.
- `F01.router-order`: fixed `/v1/<word>` routes (`capabilities`, `uploads`, `webhooks`, `kv`) reach their own routers instead of failing UUID validation on `/v1/{automation_id}`.
- `F01.base-path`: a path in `AUTOMATION_BASE_URL` moves every API route under `<path>/api/automation`; the bare probes stay at the root.

## How to get to it (agent POV)

- REST: the routes above. The bare probes (`/health`, `/ready`) are what
  container orchestration hits; the base-path twins are what a client behind
  the OpenHands ingress reaches.
- Preset runs: `presets/prompt/setup.sh` runs `curl -sf ${AUTOMATION_API_URL}/sdk-version`
  and installs `openhands-sdk==<version>` from PyPI, so a wrong answer here
  breaks every preset run.
- Agent Canvas and the `automation` skill in `OpenHands/extensions` read the
  OpenAPI document to discover request shapes.
- Config: `AUTOMATION_BASE_URL` (its path sets the base path; launch sets it
  to the service's own URL so run callbacks reach it).

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.

- **Liveness (`F01.health`).** Both paths answer without a key.
  ```sh
  control-automation api GET /health --auth none --expect 200 --check status eq ok --save F01.health/root
  control-automation api GET /api/automation/health --auth none --expect 200 --check status eq ok --save F01.health/base
  ```
- **Readiness (`F01.ready`).** The database is open, so both answer ready.
  ```sh
  control-automation api GET /ready --auth none --expect 200 --check status eq ready --save F01.ready/root
  control-automation api GET /api/automation/ready --auth none --expect 200 --check status eq ready
  ```
- **SDK version (`F01.sdk-version`).** The version a preset run installs, without a key.
  ```sh
  SDK=$(control-automation api GET /sdk-version --auth none --expect 200 --field version)
  control-automation api GET /api/automation/sdk-version --auth none --expect 200 --check version eq "$SDK" --check version matches '^[0-9]+\.[0-9]+\.[0-9]+' --save F01.sdk-version/base
  ```
- **Server info (`F01.server-info`).** Both paths agree with `/sdk-version`.
  Requires: `F01.sdk-version`
  ```sh
  control-automation api GET /server_info --auth none --expect 200 --check package_version matches '^[0-9]+\.' --check sdk_version eq "$SDK" --save F01.server-info/root
  control-automation api GET /api/automation/server_info --auth none --expect 200 --check sdk_version eq "$SDK"
  ```
- **OpenAPI document (`F01.openapi`).** Served under the base path only.
  ```sh
  control-automation api GET /api/automation/openapi.json --auth none --expect 200 --quiet --check info.title eq 'OpenHands Automations Service' --check paths exists --save F01.openapi/document
  control-automation exec --expect-output 404 -- sh -c 'curl -s -o /dev/null -w "%{http_code}" "$AUTOMATION_URL/openapi.json"'
  ```
- **Fixed routes win over the UUID route (`F01.router-order`).** Each answers its own shape, not a 422 about `automation_id`.
  ```sh
  control-automation api GET /v1/capabilities --expect 200 --check features exists
  control-automation api GET /v1/uploads --expect 200 --check uploads exists
  control-automation api GET /v1/webhooks --expect 200 --check webhooks exists
  control-automation api GET /v1/not-a-uuid --expect 422 --check detail.0.loc contains automation_id --save F01.router-order/uuid-route
  ```
- **Base path from AUTOMATION_BASE_URL (`F01.base-path`).** Restart the service with a path in its base URL, probe both layouts, then restore it.
  ```sh
  control-automation restart --service automation --env AUTOMATION_BASE_URL=http://127.0.0.1:9/qa-tenant
  control-automation exec --expect-output 200 -- sh -c 'curl -s -o /dev/null -w "%{http_code}" -H "X-Session-API-Key: $AUTOMATION_API_KEY" "$AUTOMATION_URL/qa-tenant/api/automation/v1"'
  control-automation exec --expect-output 404 -- sh -c 'curl -s -o /dev/null -w "%{http_code}" -H "X-Session-API-Key: $AUTOMATION_API_KEY" "$AUTOMATION_URL/api/automation/v1"'
  control-automation api GET /health --auth none --expect 200
  control-automation restart --service automation --reset-env
  control-automation api GET /v1 --expect 200
  ```
  Under `/qa-tenant` the API answers 200 and the default layout 404; `/health`
  stays at the root. The base URL here points at a dead port on purpose:
  only its path matters, and nothing is dispatched while it is set.

## Gotchas

- The probe failure bodies use the key `error`, not `detail` (they are plain
  `JSONResponse`s). `/ready` answers 503 `{"status":"not_ready","error":"database unavailable"}`
  only when the database cannot be opened, which a local SQLite run never
  shows.
- `package_version` is a literal in `app.py` and `__init__.py`; release
  tooling bumps both.
- `/sdk-version` reports the service's installed SDK, not the Agent
  Server's. A service running an unreleased SDK checkout breaks preset runs,
  because `setup.sh` cannot install that version from PyPI.
- `AUTOMATION_BASE_URL` also builds the callback URL handed to runs; with a
  wrong host or port, runs complete on the Agent Server but their callbacks
  never arrive (see F07 and F14). `restart --reset-env` drops every extra
  variable set with `--env`.

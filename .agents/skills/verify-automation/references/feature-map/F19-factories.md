# Factories

A factory is a named group of automations that serve one goal. It has a name
and a slug and nothing else. The slug is lowercase and URL-safe, derived from
the name when omitted, unique per org and never changes: a rename changes only
the name. An automation belongs to at most one factory, through
`automations.factory_id` (null means none, and a fresh install has no
factories). Any org member reads factories; creating, renaming and deleting
one, and assigning an automation to one, need `manage_automations`. A factory
cannot be deleted while a live automation belongs to it. The OpenHands
Enterprise server, which stores each factory's worklogs, asks this service
which factory ids exist (`GET /v1/service/factories`, behind the service key)
and is told when a factory is deleted.

Source: `openhands/automation/factory_router.py`, `openhands/automation/service_router.py`, `openhands/automation/router.py`, `openhands/automation/schemas.py`, `openhands/automation/models.py`, `openhands/automation/utils/enterprise_client.py`, `openhands/automation/utils/worklog.py`

Routes: `POST /api/automation/v1/factories`, `GET /api/automation/v1/factories`,
`GET /api/automation/v1/factories/{slug}`,
`PATCH /api/automation/v1/factories/{slug}`,
`DELETE /api/automation/v1/factories/{slug}`,
`GET /api/automation/v1/service/factories`

## Sub-features

- `F19.create`: `POST /v1/factories` with a name answers 201 with `id`, `name`, `slug` (derived: `Dark Factory` becomes `dark-factory`), `automation_count: 0`, `created_at` and `updated_at`; an explicit `slug` is kept.
- `F19.create-errors`: an invalid slug (uppercase, underscore, leading or double hyphen, over 64 characters), a blank or over-200-character name, an unknown field and a name no slug can be derived from (`!!!`) answer 422; a slug already used in the org answers 409.
- `F19.read-rename`: `GET /v1/factories` answers `{factories, total}` by name and pages with `limit`/`offset`; `GET /v1/factories/{slug}` answers one; an unknown slug answers 404; `PATCH` with a new `name` changes the name and keeps the slug, and a `slug` in the body answers 422.
- `F19.assign`: `PATCH /v1/{id}` with `factory_id` puts an automation in the factory, moves it to another, and removes it with `null`; omitting the field keeps it; every automation response carries `factory_id`; a factory id that does not exist in the org answers 404; `GET /v1?factory_id=` lists only that factory's live automations.
- `F19.count-delete`: `automation_count` counts live automations only; deleting a factory with a live automation answers 409 with the count, and once none are left answers 204 and the slug answers 404.
- `F19.service-lookup`: `GET /v1/service/factories?org_id=&id=` answers 503 `Service authentication not configured` with no service key configured, 401 `Invalid service API key` with a missing or wrong `X-Service-API-Key` (the local session key does not open it), and with the right key `{"factories": [{id, org_id}]}` for the requested ids that exist in that org; unknown ids and ids of another org are absent, and more than 100 ids answer 422.

## How to get to it (agent POV)

- REST: the five routes above, with request bodies `CreateFactoryRequest` and
  `UpdateFactoryRequest` and responses `FactoryResponse` (`schemas.py`,
  `extra="forbid"`). Assignment is `PATCH /v1/{automation_id}` with
  `factory_id` (F05), listing is `GET /v1?factory_id=`.
- Config: none. A factory is an ordinary row; there is no setting.
- Later features hang off the factory `id`, which is never reused. Other
  services keep it as a plain reference.
- Consumer: the Enterprise server calls `GET /v1/service/factories` with its
  `AUTOMATIONS_SERVICE_KEY` (the same value as this service's
  `AUTOMATION_SERVICE_KEY`), and receives `DELETE
  /api/service/orgs/{org_id}/factories/{factory_id}/worklogs` after a factory
  delete commits. A run of an automation in a factory also gets
  `AUTOMATION_WORKLOG_TOKEN` (F07).
- Git sync neither writes nor reads `factory_id` (F13): a factory's id and
  slug are local to an install.
- In local mode the only user holds `manage_automations`, so the 403 paths
  (a member creating, renaming or deleting a factory, or a creator moving
  their own automation into one) cannot be driven here; the test suite covers
  them.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.
- Bullets run in order and share one factory slug (`qa-factory`). The
  automations are inactive, so the dispatcher stays idle.

- **Create a factory (`F19.create`).** The slug is derived from the name, or kept when given.
  ```sh
  control-automation api POST /v1/factories --json '{"name":"QA Factory"}' --expect 201 --check slug eq qa-factory --check name eq 'QA Factory' --check automation_count eq 0 --save F19.create/derived
  control-automation api POST /v1/factories --json '{"name":"QA Second","slug":"qa-factory-2"}' --expect 201 --check slug eq qa-factory-2 --save F19.create/explicit
  ```
- **Refused input (`F19.create-errors`).** Each bad body is refused and a repeated slug conflicts.
  ```sh
  control-automation api POST /v1/factories --json '{"name":"x","slug":"Not_A_Slug"}' --expect 422 --save F19.create-errors/slug
  control-automation api POST /v1/factories --json '{"name":"   "}' --expect 422
  control-automation api POST /v1/factories --json '{"name":"x","description":"no"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api POST /v1/factories --json '{"name":"!!!"}' --expect 422 --check detail contains 'slug cannot be derived'
  control-automation api POST /v1/factories --json '{"name":"QA   factory"}' --expect 409 --check detail contains qa-factory --save F19.create-errors/conflict
  ```
- **Read and rename (`F19.read-rename`).** The slug stays when the name changes.
  Requires: `F19.create`
  ```sh
  control-automation api GET /v1/factories --expect 200 --check total ge 2 --save F19.read-rename/list
  control-automation api GET /v1/factories/qa-factory --expect 200 --check name eq 'QA Factory' --save F19.read-rename/get
  control-automation api PATCH /v1/factories/qa-factory --json '{"name":"QA Renamed"}' --expect 200 --check name eq 'QA Renamed' --check slug eq qa-factory --save F19.read-rename/rename
  control-automation api PATCH /v1/factories/qa-factory --json '{"name":"x","slug":"other"}' --expect 422
  control-automation api GET /v1/factories/no-such-factory --expect 404 --check detail eq 'Factory not found'
  ```
- **Assign, move and remove an automation (`F19.assign`).** Another org's or an unknown factory id is a 404.
  Requires: `F19.create`
  ```sh
  FA=$(control-automation api GET /v1/factories/qa-factory --expect 200 --field id)
  FB=$(control-automation api GET /v1/factories/qa-factory-2 --expect 200 --field id)
  AID=$(control-automation api POST /v1 --json '{"name":"qa-factory-member","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","state":"INACTIVE"}' --expect 201 --check factory_id eq null --field id)
  control-automation api PATCH "/v1/$AID" --json "{\"factory_id\":\"$FA\"}" --expect 200 --check factory_id eq "$FA" --save F19.assign/set
  control-automation api GET /v1 --query factory_id="$FA" --expect 200 --check total eq 1 --check automations.0.id eq "$AID" --save F19.assign/filter
  control-automation api PATCH "/v1/$AID" --json "{\"factory_id\":\"$FB\"}" --expect 200 --check factory_id eq "$FB"
  control-automation api PATCH "/v1/$AID" --json '{"name":"qa-factory-member-2"}' --expect 200 --check factory_id eq "$FB"
  control-automation api PATCH "/v1/$AID" --json '{"factory_id":"00000000-0000-4000-8000-000000000000"}' --expect 404 --check detail eq 'Factory not found' --save F19.assign/unknown
  control-automation api PATCH "/v1/$AID" --json '{"factory_id":null}' --expect 200 --check factory_id eq null --save F19.assign/clear
  ```
- **Counts and deletion (`F19.count-delete`).** A live automation blocks deletion; a soft-deleted one does not. Removes both factories.
  Requires: `F19.create`
  ```sh
  DID=$(control-automation api POST /v1 --json '{"name":"qa-factory-delete","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","state":"INACTIVE"}' --expect 201 --field id)
  FA=$(control-automation api GET /v1/factories/qa-factory --expect 200 --field id)
  control-automation api PATCH "/v1/$DID" --json "{\"factory_id\":\"$FA\"}" --expect 200
  control-automation api GET /v1/factories/qa-factory --expect 200 --check automation_count eq 1 --save F19.count-delete/count
  control-automation api DELETE /v1/factories/qa-factory --expect 409 --check detail contains '1 live automation' --save F19.count-delete/blocked
  control-automation api DELETE "/v1/$DID" --expect 204
  control-automation api GET /v1/factories/qa-factory --expect 200 --check automation_count eq 0
  control-automation api DELETE /v1/factories/qa-factory --expect 204 --save F19.count-delete/delete
  control-automation api GET /v1/factories/qa-factory --expect 404
  control-automation state sql "SELECT factory_id FROM automations WHERE id = replace('$DID','-','')" --check rows.0.factory_id eq null
  control-automation api DELETE /v1/factories/qa-factory-2 --expect 204
  ```
  The deleted automation's row is still in the table; the factory delete
  cleared its `factory_id`.
- **Lookup for another service (`F19.service-lookup`).** The service key opens the route; every other caller is refused. Restores the baseline.
  ```sh
  control-automation api GET '/v1/service/factories?org_id=00000000-0000-4000-8000-000000000000' --auth none --expect 503 --check detail eq 'Service authentication not configured' --save F19.service-lookup/unconfigured
  control-automation restart --service automation --env AUTOMATION_SERVICE_KEY=qa-service-key
  control-automation api GET '/v1/service/factories?org_id=00000000-0000-4000-8000-000000000000' --auth none --expect 401 --check detail eq 'Invalid service API key' --save F19.service-lookup/missing
  control-automation api GET '/v1/service/factories?org_id=00000000-0000-4000-8000-000000000000' --auth key --expect 401 --check detail eq 'Invalid service API key'
  FS=$(control-automation api POST /v1/factories --json '{"name":"QA Service Lookup"}' --expect 201 --field id)
  ORG=$(control-automation state sql "SELECT org_id FROM factories WHERE slug = 'qa-service-lookup'" --field rows.0.org_id)
  control-automation api GET "/v1/service/factories?org_id=$ORG&id=$FS&id=00000000-0000-4000-8000-000000000001" --auth none --header 'X-Service-API-Key: qa-service-key' --expect 200 --check factories.0.id eq "$FS" --save F19.service-lookup/found
  control-automation api GET "/v1/service/factories?org_id=00000000-0000-4000-8000-000000000000&id=$FS" --auth none --header 'X-Service-API-Key: qa-service-key' --expect 200 --check factories eq '[]' --save F19.service-lookup/other-org
  control-automation api DELETE /v1/factories/qa-service-lookup --expect 204
  control-automation restart --service automation --reset-env
  ```
  The `org_id` is read from the table because the local user's org is not
  fixed. In local mode no notification is sent to an enterprise server when
  the factory is deleted, so the run needs no stub for it.

## Gotchas

- Assigning a factory needs `manage_automations` even for the automation's
  creator, who may otherwise edit it. Resending the factory the automation
  already has is not a change and needs no permission.
- `PATCH /v1/{id}` with `factory_id: null` removes the automation from its
  factory; leaving the field out keeps it.
- Deleting a factory never deletes automations: it is refused while a live one
  belongs to it. Soft-deleted automations that pointed at it end with
  `factory_id` null.
- The router is registered before `/v1/{automation_id}`, which would
  otherwise take `factories` as an id (F01 router order).
- A run of an automation in a factory gets `AUTOMATION_WORKLOG_TOKEN` only in
  cloud mode with a service key, never in local mode. The run keeps the factory
  named in its token until the token expires (24 hours), even if the
  automation moves to another factory meanwhile.

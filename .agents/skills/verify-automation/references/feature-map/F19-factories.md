# Factories

A factory is a named group of automations. An organization creates one with a
name (the URL slug is derived from it or supplied, unique per org and never
changes), renames it, lists and reads it with a count of its live
automations, and deletes it once no live automation belongs to it. An
automation joins, changes or leaves a factory through `PATCH /v1/{id}` with
`factory_id`, and `GET /v1?factory_id=` lists a factory's automations. A fresh
install has no factory. The local user holds `manage_automations`, so the
permission refusals are not reachable here.

Source: `openhands/automation/factory_router.py`, `openhands/automation/factories.py`, `openhands/automation/router.py`, `openhands/automation/schemas.py`, `openhands/automation/models.py`

Routes: `POST /api/automation/v1/factories`, `GET /api/automation/v1/factories`,
`GET /api/automation/v1/factories/{slug}`,
`PATCH /api/automation/v1/factories/{slug}`,
`DELETE /api/automation/v1/factories/{slug}`,
`GET /api/automation/v1/service/factories`

## Sub-features

- `F19.create`: `POST /v1/factories` with only a `name` answers 201 with `id`, `name`, the derived lowercase `slug`, `automation_count: 0`, `created_at` and `updated_at`; a GET by slug returns the same record, and a fresh install lists none.
- `F19.slug`: a supplied `slug` is lowercased and kept; a bad slug, a name with nothing to derive a slug from, a blank name and an unknown field each answer 422; a second factory with the same slug answers 409 `Factory slug '<slug>' already exists in this org`.
- `F19.rename`: `PATCH /v1/factories/{slug}` changes `name` and bumps `updated_at`; the slug and id stay, and a body that carries `slug` answers 422.
- `F19.assign`: `PATCH /v1/{id}` with `factory_id` assigns the automation, a second factory moves it, `null` removes it, and omitting the field leaves it; every automation response carries `factory_id`, and an unknown factory id answers 404 `Factory not found`.
- `F19.list-filter`: `GET /v1?factory_id=<id>` returns only that factory's live automations, and the factory's `automation_count` counts the same ones.
- `F19.delete`: `DELETE /v1/factories/{slug}` answers 409 `Factory has N live automations; remove them from it first` while any live automation belongs to it, and 204 once none does; the factory is then 404 and the same slug yields a new id.
- `F19.lookup`: `GET /v1/service/factories?org_id=<org>&id=<id>...` with the service key answers 200 `{"factories": [{"id", "org_id"}]}` holding only the requested ids that exist in that org; a missing or wrong `X-Service-API-Key` answers 401, no configured service key 503, and more than 100 ids 422.
- `F19.audit`: each `factory_id` change appears in `GET /v1/{id}/changes` with `factory_id` in `changed_fields`.

## How to get to it (agent POV)

- REST: the five factory routes above plus `factory_id` on `PATCH /v1/{id}` and
  as a `GET /v1` filter, all behind the local API key (`X-Session-API-Key` or
  `Authorization: Bearer`). Create, rename, delete and a `factory_id` change
  need `manage_automations` (the local user has it); reads need
  `view_automations`.
- CLI: `control-automation api POST /v1/factories --json '{"name":"..."}'`.
- A factory has no description or other attribute, and no built-in default.
- Git sync neither writes nor reads `factory_id`.
- Enterprise reads factories through `GET /v1/service/factories`, with
  `X-Service-API-Key: <AUTOMATION_SERVICE_KEY>` (its `AUTOMATIONS_SERVICE_KEY`),
  to confirm a factory exists. After a delete this service calls Enterprise's
  worklog purge route; local mode has no Enterprise server, so nothing is sent
  and a recipe cannot observe it (the unit tests do).
- A run of an automation that belongs to a factory gets
  `AUTOMATION_WORKLOG_TOKEN` in cloud mode when the service key is set; local
  mode never does.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM, no network.
- Factory names and slugs are prefixed `qa-`. The automations use an external
  `tarball_path` that is never fetched, so no upload is needed.

- **Create and read (`F19.create`).** One name is enough; the slug is derived.
  ```sh
  control-automation api GET /v1/factories --expect 200 --check total eq 0 --check factories len-eq 0
  control-automation api POST /v1/factories --json '{"name":"QA Factory One"}' --expect 201 --check slug eq qa-factory-one --check name eq 'QA Factory One' --check automation_count eq 0 --save F19.create/create
  control-automation api GET /v1/factories/qa-factory-one --expect 200 --check slug eq qa-factory-one --check automation_count eq 0 --check description missing --save F19.create/get
  control-automation api GET /v1/factories --expect 200 --check total eq 1 --check factories.0.slug eq qa-factory-one
  ```
  The first list is empty only on a fresh run; a run that already holds
  factories reports their count there instead.
- **Slug rules (`F19.slug`).** Supplied slugs are validated, derived ones must exist, and a slug is unique per org.
  ```sh
  control-automation api POST /v1/factories --json '{"name":"QA Two","slug":"QA-Two"}' --expect 201 --check slug eq qa-two --save F19.slug/supplied
  control-automation api POST /v1/factories --json '{"name":"QA Dup","slug":"qa-two"}' --expect 409 --check detail eq "Factory slug 'qa-two' already exists in this org" --save F19.slug/duplicate
  control-automation api POST /v1/factories --json '{"name":"qa two"}' --expect 409 --check detail contains "'qa-two'"
  control-automation api POST /v1/factories --json '{"name":"qa","slug":"not_a_slug"}' --expect 422 --check detail.0.loc contains slug
  control-automation api POST /v1/factories --json '{"name":"!!!"}' --expect 422 --check detail contains 'provide a slug'
  control-automation api POST /v1/factories --json '{"name":"   "}' --expect 422
  control-automation api POST /v1/factories --json '{"name":"qa","description":"x"}' --expect 422 --check detail.0.type eq extra_forbidden
  ```
- **Rename (`F19.rename`).** The name changes and the slug does not.
  ```sh
  control-automation api PATCH /v1/factories/qa-two --json '{"name":"QA Two Renamed"}' --expect 200 --check name eq 'QA Two Renamed' --check slug eq qa-two --save F19.rename/rename
  control-automation api PATCH /v1/factories/qa-two --json '{"name":"x","slug":"other"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api GET /v1/factories/qa-two --expect 200 --check name eq 'QA Two Renamed' --check slug eq qa-two
  control-automation api GET /v1/factories/other --expect 404 --check detail eq 'Factory not found'
  ```
- **Assign, move and clear (`F19.assign`).** Membership is a PATCH of the automation; the response and a GET carry it.
  ```sh
  F1=$(control-automation api GET /v1/factories/qa-factory-one --expect 200 --field id)
  F2=$(control-automation api GET /v1/factories/qa-two --expect 200 --field id)
  AID=$(control-automation api POST /v1 --json '{"name":"qa-factory-member","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --check factory_id eq null --field id)
  control-automation api PATCH "/v1/$AID" --json "{\"factory_id\":\"$F1\"}" --expect 200 --check factory_id eq "$F1" --save F19.assign/assign
  control-automation api PATCH "/v1/$AID" --json "{\"factory_id\":\"$F2\"}" --expect 200 --check factory_id eq "$F2"
  control-automation api PATCH "/v1/$AID" --json '{"name":"qa-factory-member-2"}' --expect 200 --check factory_id eq "$F2"
  control-automation api GET "/v1/$AID" --expect 200 --check factory_id eq "$F2"
  control-automation api PATCH "/v1/$AID" --json '{"factory_id":"00000000-0000-4000-8000-000000000000"}' --expect 404 --check detail eq 'Factory not found' --save F19.assign/unknown
  control-automation api PATCH "/v1/$AID" --json "{\"factory_id\":\"$F1\"}" --expect 200 --check factory_id eq "$F1"
  ```
  Leaves the automation in `qa-factory-one`.
- **List filter and count (`F19.list-filter`).** The filter and the factory's count agree.
  Requires: `F19.assign`
  ```sh
  control-automation api GET /v1 --query factory_id="$F1" --expect 200 --check total eq 1 --check automations.0.id eq "$AID" --check automations.0.factory_id eq "$F1" --save F19.list-filter/filtered
  control-automation api GET /v1 --query factory_id="$F2" --expect 200 --check total eq 0 --check automations len-eq 0
  control-automation api GET /v1/factories/qa-factory-one --expect 200 --check automation_count eq 1
  control-automation api GET /v1/factories/qa-two --expect 200 --check automation_count eq 0
  ```
- **Delete (`F19.delete`).** Refused while a live automation belongs to the factory, allowed after it is removed from it.
  Requires: `F19.list-filter`
  ```sh
  control-automation api DELETE /v1/factories/qa-factory-one --expect 409 --check detail eq 'Factory has 1 live automation; remove them from it first' --save F19.delete/refused
  control-automation api PATCH "/v1/$AID" --json '{"factory_id":null}' --expect 200 --check factory_id eq null
  control-automation api DELETE /v1/factories/qa-factory-one --expect 204 --save F19.delete/deleted
  control-automation api GET /v1/factories/qa-factory-one --expect 404 --check detail eq 'Factory not found'
  control-automation api POST /v1/factories --json '{"name":"QA Factory One"}' --expect 201 --check slug eq qa-factory-one --check id ne "$F1"
  control-automation api DELETE /v1/factories/qa-factory-one --expect 204
  control-automation api DELETE /v1/factories/qa-two --expect 204
  ```
  Leaves no factory behind. A soft-deleted automation does not block the
  delete; it is detached in the same transaction.
- **Service lookup (`F19.lookup`).** Only the service key opens it, and only ids that exist in the org come back. Restores the baseline.
  Requires: `F19.create`
  ```sh
  control-automation restart --service automation --env AUTOMATION_SERVICE_KEY=qa-service-key
  LORG=46d09c5c-7d7d-585f-8c37-a6b45a3de738
  LF=$(control-automation api POST /v1/factories --json '{"name":"QA Lookup"}' --expect 201 --field id)
  control-automation api GET /v1/service/factories --auth none --header 'X-Service-API-Key: qa-service-key' --query org_id="$LORG" --query id="$LF" --query id=00000000-0000-4000-8000-000000000000 --expect 200 --check factories len-eq 1 --check factories.0.id eq "$LF" --check factories.0.org_id eq "$LORG" --save F19.lookup/found
  control-automation api GET /v1/service/factories --auth none --header 'X-Service-API-Key: qa-service-key' --query org_id=00000000-0000-4000-8000-000000000000 --query id="$LF" --expect 200 --check factories len-eq 0
  control-automation api GET /v1/service/factories --auth none --query org_id="$LORG" --query id="$LF" --expect 401 --check detail eq 'Invalid service API key' --save F19.lookup/missing-key
  control-automation api GET /v1/service/factories --auth none --header 'X-Service-API-Key: not-the-key' --query org_id="$LORG" --query id="$LF" --expect 401
  control-automation api DELETE "/v1/factories/qa-lookup" --expect 204
  control-automation restart --service automation --reset-env
  control-automation api GET /v1/service/factories --auth none --header 'X-Service-API-Key: qa-service-key' --query org_id="$LORG" --query id="$LF" --expect 503 --check detail eq 'Service authentication not configured' --save F19.lookup/unconfigured
  ```
  The over-100-ids refusal (422) needs 101 query parameters and is covered by
  the unit tests.
- **Change audit (`F19.audit`).** Each membership change is one change row listing `factory_id`.
  Requires: `F19.assign`
  ```sh
  control-automation api GET "/v1/$AID/changes" --expect 200 --check changes.0.changed_fields contains factory_id --save F19.audit/changes
  ```

## Gotchas

- The slug is immutable and the only handle in the URL; the `id` is what
  other services and `PATCH /v1/{id}` use. A deleted factory's id is never
  handed out again.
- Setting `factory_id` is a factory-level act: it needs `manage_automations`
  even for the automation's own creator, unlike the other fields of a PATCH.
  The local key holds the permission, so that refusal needs an org member
  and is covered by the unit tests, not here.
- `automation_count` and the delete check count live (not soft-deleted)
  automations only, in any state, including drafts.
- `GET /v1/factories` pages with `limit` (1 to 100) and `offset` like the
  webhook list, newest first.

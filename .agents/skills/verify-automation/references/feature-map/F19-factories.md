# Factories

A factory is a named group of automations. An organization creates one with a
name (addressed by its id; names need not be unique), renames it, lists and reads it with a count of its live
automations, and deletes it once no live automation belongs to it. An
automation joins a factory when it is created (`factory_id` on `POST /v1`,
`/v1/plugin-spec`, `/v1/preset/prompt` and `/v1/preset/plugin`), and changes or
leaves one through `PATCH /v1/{id}` with `factory_id`. `GET /v1/capabilities`
lists `factories` in `features`. And `GET /v1?factory_id=` lists a factory's automations. A fresh
install has no factory. The local user holds `manage_automations`, so the
permission refusals are not reachable here.

Source: `openhands/automation/factory_router.py`, `openhands/automation/router.py`, `openhands/automation/schemas.py`, `openhands/automation/models.py`

Routes: `POST /api/automation/v1/factories`, `GET /api/automation/v1/factories`,
`GET /api/automation/v1/factories/{factory_id}`,
`PATCH /api/automation/v1/factories/{factory_id}`,
`DELETE /api/automation/v1/factories/{factory_id}`

## Sub-features

- `F19.create`: `POST /v1/factories` with only a `name` answers 201 with `id`, `name`, `automation_count: 0`, `created_at` and `updated_at`; a GET by id returns the same record, and a fresh install lists none.
- `F19.create-in`: `factory_id` on each create endpoint assigns the new automation; an unknown factory answers 404 `Factory not found`. A blank name, an unknown field (including `slug`) answer 422, and two factories may share a name.
- `F19.rename`: `PATCH /v1/factories/{factory_id}` changes `name` and bumps `updated_at`; the id stays, and a body that carries `slug` answers 422.
- `F19.assign`: `PATCH /v1/{id}` with `factory_id` assigns the automation, a second factory moves it, `null` removes it, and omitting the field leaves it; every automation response carries `factory_id`, and an unknown factory id answers 404 `Factory not found`.
- `F19.list-filter`: `GET /v1?factory_id=<id>` returns only that factory's live automations, and the factory's `automation_count` counts the same ones.
- `F19.delete`: `DELETE /v1/factories/{factory_id}` answers 409 `Factory has N live automations; remove them from it first` while any live automation belongs to it, and 204 once none does; the factory is then 404 and a new factory with the same name gets a new id.
- `F19.audit`: each `factory_id` change appears in `GET /v1/{id}/changes` with `factory_id` in `changed_fields`.

## How to get to it (agent POV)

- REST: the five factory routes above plus `factory_id` on the create endpoints, `PATCH /v1/{id}` and
  as a `GET /v1` filter, all behind the local API key (`X-Session-API-Key` or
  `Authorization: Bearer`). Create, rename, delete and a `factory_id` change
  need `manage_automations` (the local user has it); reads need
  `view_automations`.
- CLI: `control-automation api POST /v1/factories --json '{"name":"..."}'`.
- A factory has no description or other attribute, and no built-in default.
- Git sync neither writes nor reads `factory_id`.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM, no network.
- Factory names are prefixed `qa-`. The automations use an external
  `tarball_path` that is never fetched, so no upload is needed.

- **Create and read (`F19.create`).** One name is enough.
  ```sh
  control-automation api GET /v1/factories --expect 200 --check total eq 0 --check factories len-eq 0
  F1=$(control-automation api POST /v1/factories --json '{"name":"QA Factory One"}' --expect 201 --check name eq 'QA Factory One' --check automation_count eq 0 --check slug missing --save F19.create/create --field id)
  control-automation api GET "/v1/factories/$F1" --expect 200 --check automation_count eq 0 --check description missing --save F19.create/get
  control-automation api GET /v1/factories --expect 200 --check total eq 1 --check factories.0.id eq "$F1"
  ```
  The first list is empty only on a fresh run; a run that already holds
  factories reports their count there instead.
- **Input rules and duplicate names (`F19.create-in`).** Bad input is refused; a repeated name is allowed.
  ```sh
  control-automation api POST /v1/factories --json '{"name":"qa","slug":"x"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api POST /v1/factories --json '{"name":"   "}' --expect 422
  control-automation api POST /v1/factories --json '{"name":"qa","description":"x"}' --expect 422 --check detail.0.type eq extra_forbidden
  F2=$(control-automation api POST /v1/factories --json '{"name":"QA Two"}' --expect 201 --field id)
  F2B=$(control-automation api POST /v1/factories --json '{"name":"QA Two"}' --expect 201 --check id ne "$F2" --field id)
  control-automation api DELETE "/v1/factories/$F2B" --expect 204
  CID=$(control-automation api POST /v1 --json "{\"name\":\"qa-factory-created\",\"factory_id\":\"$F2\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"https://qa.invalid/x.tgz\",\"entrypoint\":\"bash run.sh\"}" --expect 201 --check factory_id eq "$F2" --field id)
  control-automation api POST /v1 --json '{"name":"qa-factory-unknown","factory_id":"00000000-0000-4000-8000-000000000000","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 404 --check detail eq 'Factory not found'
  control-automation api DELETE "/v1/$CID" --expect 204
  ```
- **Rename (`F19.rename`).** The name changes and the id does not.
  Requires: `F19.create`, `F19.create-in`
  ```sh
  control-automation api PATCH "/v1/factories/$F2" --json '{"name":"QA Two Renamed"}' --expect 200 --check name eq 'QA Two Renamed' --check id eq "$F2" --save F19.rename/rename
  control-automation api PATCH "/v1/factories/$F2" --json '{"name":"x","slug":"other"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api GET "/v1/factories/$F2" --expect 200 --check name eq 'QA Two Renamed'
  control-automation api GET /v1/factories/other --expect 422
  ```
- **Assign, move and clear (`F19.assign`).** Membership is a PATCH of the automation; the response and a GET carry it.
  Requires: `F19.create`, `F19.create-in`
  ```sh
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
  control-automation api GET "/v1/factories/$F1" --expect 200 --check automation_count eq 1
  control-automation api GET "/v1/factories/$F2" --expect 200 --check automation_count eq 0
  ```
- **Delete (`F19.delete`).** Refused while a live automation belongs to the factory, allowed after it is removed from it.
  Requires: `F19.list-filter`
  ```sh
  control-automation api DELETE "/v1/factories/$F1" --expect 409 --check detail eq 'Factory has 1 live automation; remove them from it first' --save F19.delete/refused
  control-automation api PATCH "/v1/$AID" --json '{"factory_id":null}' --expect 200 --check factory_id eq null
  control-automation api DELETE "/v1/factories/$F1" --expect 204 --save F19.delete/deleted
  control-automation api GET "/v1/factories/$F1" --expect 404 --check detail eq 'Factory not found'
  control-automation api POST /v1/factories --json '{"name":"QA Factory One"}' --expect 201 --check id ne "$F1"
  control-automation api DELETE "/v1/factories/$F1" --expect 204
  control-automation api DELETE "/v1/factories/$F2" --expect 204
  ```
  Leaves no factory behind. A soft-deleted automation does not block the
  delete; it is detached in the same transaction.
- **Change audit (`F19.audit`).** Each membership change is one change row listing `factory_id`.
  Requires: `F19.assign`
  ```sh
  control-automation api GET "/v1/$AID/changes" --expect 200 --check changes.0.changed_fields contains factory_id --save F19.audit/changes
  ```

## Gotchas

- The `id` is the only handle for a factory, in URLs and for other services.
  A deleted factory's id is never handed out again.
- Setting `factory_id` is a factory-level act: it needs `manage_automations`
  even for the automation's own creator, unlike the other fields of a PATCH,
  and on every create endpoint that accepts `factory_id`. A draft stores the
  field but its hidden test-run automation never carries it, so a draft cannot
  block a factory delete.
  The local key holds the permission, so that refusal needs an org member
  and is covered by the unit tests, not here.
- `automation_count` and the delete check count live (not soft-deleted)
  automations only, in any state, including drafts.
- `GET /v1/factories` pages with `limit` (1 to 100) and `offset` like the
  webhook list, newest first.

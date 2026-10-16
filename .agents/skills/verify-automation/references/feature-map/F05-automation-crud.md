# Automation CRUD and tarball download

The raw automation API: a client that ships its own code creates an
automation from a `tarball_path` (an internal upload from F04 or a public
`https://`, `s3://` or `gs://` URL), an `entrypoint` command, a trigger (cron
or event) and optional run settings, then lists, reads, edits, soft-deletes
it and downloads the tarball it runs. Creation validates every field
strictly (unknown fields are refused, entrypoints are checked for absolute
paths, traversal and shell metacharacters, timeouts are bounded, cron and
timezone are parsed) and is idempotent per `template.id`. PATCH applies the
same validators, turns `state`/`enabled` into the disable bookkeeping
(`disabled_reason`, `disabled_detail`, `disabled_at`) and keeps
`keep_alive` true for `continue_conversation` triggers.

Source: `openhands/automation/router.py`, `openhands/automation/schemas.py`, `openhands/automation/utils/tarball_validation.py`, `openhands/automation/utils/templates.py`, `openhands/automation/utils/timeout.py`, `openhands/automation/utils/cron.py`, `openhands/automation/utils/state.py`, `openhands/automation/utils/model_profiles.py`

Routes: `POST /api/automation/v1`, `GET /api/automation/v1`,
`GET /api/automation/v1/{automation_id}`, `PATCH /api/automation/v1/{automation_id}`,
`DELETE /api/automation/v1/{automation_id}`,
`GET /api/automation/v1/{automation_id}/tarball`

## Sub-features

- `F05.create-internal`: `POST /v1` with an internal upload answers 201 with the defaults `state: ACTIVE`, `enabled: true`, `timeout: 600`, trigger `timezone: UTC`, `keep_alive`, `prompt`, `preset_metadata` and `model` null; a GET returns the same record.
- `F05.create-external`: an `https://` tarball URL is accepted by scheme alone (not fetched) and stored as given.
- `F05.tarball-path-errors`: `ftp://` answers 422 from the schema, `http://` 422 `Invalid tarball_path...`, a malformed `oh-internal://` URL 422 `Invalid internal upload URL format...`, an unknown upload id 404 `Upload not found`.
- `F05.entrypoint-rules`: an absolute path, `..`, a shell metacharacter, a blank or a missing `entrypoint` answers 422 with the matching message; spaces are allowed (`uv run main.py`).
- `F05.timeout-bounds`: `timeout` 0 answers 422 `timeout must be a positive number`, 1801 answers 422 `...must not exceed 1800 seconds (30 minutes)`, 1800 is stored.
- `F05.cron-timezone`: an invalid cron, a cron with no future fire time (`0 0 31 2 *`), an unknown timezone and a trigger without `type` answer 422; a valid IANA timezone is stored.
- `F05.extra-fields`: an unknown body field answers 422 `extra_forbidden`; an empty `name` answers 422.
- `F05.state-enabled`: `state: INACTIVE` or `enabled: false` creates an inactive automation; `state: ACTIVE` with `enabled: false` and `state: DRAFT` answer 422.
- `F05.profile-model`: `agent_profile_id` together with `model` answers 422 `An agent profile already specifies the model`; a profile alone stores `model: null`; in local mode any pattern-valid `model` is stored as given.
- `F05.keep-alive-continue`: an event trigger with `destination: continue_conversation` forces `keep_alive: true` on create; `keep_alive: false` with it answers 422.
- `F05.template-idempotent`: a create with `template` answers 201 and records `preset_metadata.template`; a repeat with the same `template.id` answers 200 with the same automation unchanged (the new body is ignored).
- `F05.list`: `GET /v1` answers `{automations, total}` newest first, pages with `limit`/`offset`, filters `created_by=me|others`, and rejects out-of-range `limit` and unknown `created_by` with 422.
- `F05.get`: an unknown id answers 404 `Automation not found`, a non-UUID 422.
- `F05.patch-fields`: PATCH updates `name`, `trigger`, `timeout`, `entrypoint`, `setup_script_path` and bumps `updated_at`; unset fields are kept; a GET shows the stored values.
- `F05.patch-state`: PATCH `state: INACTIVE` sets `enabled: false`, `disabled_reason: manual`, `disabled_detail {"reason":"manual","source":"user"}`, `disabled_at` and records a disable event; `enabled: true` reactivates and clears all three.
- `F05.patch-validation`: PATCH applies the create validators (timeout bounds, entrypoint rules, unknown fields, `state: DRAFT`) with 422; an unknown id answers 404; `keep_alive: false` on a `continue_conversation` automation answers 400.
- `F05.patch-null`, known bug: an explicit `null` for a NOT NULL column (`name`, `entrypoint`, `tarball_path`, `trigger`, `enabled`) answers 500 instead of 422.
- `F05.delete`: DELETE answers 204 and soft-deletes (`deleted_at`, `state: INACTIVE`, `disabled_reason: manual_delete`); afterwards GET, PATCH and DELETE answer 404 and the list drops it.
- `F05.tarball-download`: `GET /v1/{id}/tarball` for an internal upload answers 200 `application/x-tar` with `Content-Disposition: attachment; filename="<name>.tar"` and the uploaded bytes unchanged.
- `F05.tarball-filename`: a non-ASCII name gets an ASCII `filename` fallback plus an RFC 5987 `filename*`; quotes and slashes are stripped.
- `F05.tarball-external`: an `https://` tarball answers 302 to the URL; `s3://` and `gs://` answer 422 `Cannot proxy s3:// tarball URLs...`.
- `F05.auth`: every route answers 401 without a key or with a wrong key.

## How to get to it (agent POV)

- REST: the six routes above. Request bodies are `CreateAutomationRequest`
  and `UpdateAutomationRequest` (`schemas.py`, `extra="forbid"`); responses
  are `AutomationResponse`.
- CLI: `control-automation api POST /v1 --json '{...}'`;
  `control-automation fixture automation --kind echo --field id` uploads a
  stdlib fixture and creates a yearly-cron automation from it in one step;
  `fixture tarball --kind echo --upload --field tarball_path` gives just the
  internal path.
- The `automation` skill in `OpenHands/extensions` creates raw automations
  this way (with `template` provenance for one-click enables); Agent Canvas
  lists, edits, disables and deletes them through the same routes.
- Config: `AUTOMATION_DEFAULT_RUN_DURATION` (600) and
  `AUTOMATION_MAX_RUN_DURATION` (1800) set the timeout default and bound;
  local mode (`AUTOMATION_AGENT_SERVER_URL` set) skips model and agent
  profile existence checks.
- Presets (F06) and drafts create automations of the same shape through
  their own routes; this family's GET, PATCH, DELETE and tarball routes
  serve them too.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.
- Every bullet builds its own fixture; none of the automations here fires
  (yearly cron `0 0 1 1 *`, or `state: INACTIVE`), so the dispatcher stays
  idle.

- **Create from an internal upload (`F05.create-internal`).** Defaults filled in, read back by GET.
  ```sh
  TP=$(control-automation fixture tarball --kind echo --upload --field tarball_path)
  AID=$(control-automation api POST /v1 --json "{\"name\":\"qa-crud\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\"}" --expect 201 --check state eq ACTIVE --check enabled eq true --check timeout eq 600 --check trigger.timezone eq UTC --check keep_alive eq null --check prompt eq null --check preset_metadata eq null --check model eq null --save F05.create-internal/create --field id)
  control-automation api GET "/v1/$AID" --expect 200 --check name eq qa-crud --check tarball_path eq "$TP" --check entrypoint eq 'bash run.sh' --check trigger.schedule eq '0 0 1 1 *' --check disabled_reason eq null --save F05.create-internal/get
  ```
- **Create from an external URL (`F05.create-external`).** The URL is not fetched at create time.
  ```sh
  EID=$(control-automation api POST /v1 --json '{"name":"qa-external","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/qa.tar.gz","entrypoint":"bash run.sh"}' --expect 201 --save F05.create-external/create --field id)
  control-automation api GET "/v1/$EID" --expect 200 --check tarball_path eq https://qa.invalid/qa.tar.gz
  ```
  `qa.invalid` never resolves; the dispatcher would fail the first run at
  download time.
- **tarball_path errors (`F05.tarball-path-errors`).** Schema, scheme, format and existence, each refused.
  ```sh
  control-automation api POST /v1 --json '{"name":"qa-bad-tp","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"ftp://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail.0.loc.1 eq tarball_path --check detail.0.msg contains 'tarball_path must start with s3://, gs://, http://, or https://' --save F05.tarball-path-errors/ftp
  control-automation api POST /v1 --json '{"name":"qa-bad-tp","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"http://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail eq 'Invalid tarball_path. Must be oh-internal://uploads/{uuid} or a public URL (https://, s3://, gs://)' --save F05.tarball-path-errors/http
  control-automation api POST /v1 --json '{"name":"qa-bad-tp","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"oh-internal://uploads/not-a-uuid","entrypoint":"bash run.sh"}' --expect 422 --check detail eq 'Invalid internal upload URL format. Expected: oh-internal://uploads/{uuid}'
  control-automation api POST /v1 --json '{"name":"qa-bad-tp","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"oh-internal://uploads/00000000-0000-4000-8000-000000000000","entrypoint":"bash run.sh"}' --expect 404 --check detail eq 'Upload not found'
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name = 'qa-bad-tp'" --check rows.0.n eq 0
  ```
  `http://` passes the schema regex and is refused by the handler's scheme
  check; the schema message never mentions `oh-internal://`, which it
  nevertheless accepts.
- **Entrypoint rules (`F05.entrypoint-rules`).** Each unsafe form has its own message; spaces are fine.
  ```sh
  control-automation api POST /v1 --json '{"name":"qa-ep","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"/bin/run"}' --expect 422 --check detail.0.msg eq 'Value error, entrypoint must be a relative path, not an absolute path' --save F05.entrypoint-rules/absolute
  control-automation api POST /v1 --json '{"name":"qa-ep","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"../run.sh"}' --expect 422 --check detail.0.msg eq 'Value error, entrypoint must not contain path traversal (..)'
  control-automation api POST /v1 --json '{"name":"qa-ep","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh; rm -rf x"}' --expect 422 --check detail.0.msg contains 'entrypoint must not contain shell metacharacters'
  control-automation api POST /v1 --json '{"name":"qa-ep","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"   "}' --expect 422 --check detail.0.msg eq 'Value error, entrypoint must not be blank'
  control-automation api POST /v1 --json '{"name":"qa-ep","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz"}' --expect 422 --check detail.0.type eq missing --check detail.0.loc.1 eq entrypoint
  control-automation api POST /v1 --json '{"name":"qa-ep-setup","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","setup_script_path":"/etc/setup.sh"}' --expect 422 --check detail.0.msg contains 'setup_script_path must be a relative path'
  control-automation api POST /v1 --json '{"name":"qa-ep-ok","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"uv run main.py","setup_script_path":"scripts/setup.sh"}' --expect 201 --check entrypoint eq 'uv run main.py' --check setup_script_path eq scripts/setup.sh
  ```
  `setup_script_path` goes through the same checks.
- **Timeout bounds (`F05.timeout-bounds`).** Positive and at most `AUTOMATION_MAX_RUN_DURATION`.
  ```sh
  control-automation api POST /v1 --json '{"name":"qa-timeout","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","timeout":0}' --expect 422 --check detail.0.msg eq 'Value error, timeout must be a positive number' --save F05.timeout-bounds/zero
  control-automation api POST /v1 --json '{"name":"qa-timeout","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","timeout":1801}' --expect 422 --check detail.0.msg eq 'Value error, timeout must not exceed 1800 seconds (30 minutes)' --save F05.timeout-bounds/over
  TID=$(control-automation api POST /v1 --json '{"name":"qa-timeout","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","timeout":1800}' --expect 201 --field id)
  control-automation api GET "/v1/$TID" --expect 200 --check timeout eq 1800
  ```
- **Cron and timezone (`F05.cron-timezone`).** Parsed at create time; a valid timezone is stored.
  ```sh
  control-automation api POST /v1 --json '{"name":"qa-cron","trigger":{"type":"cron","schedule":"not a cron"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail.0.loc contains schedule --check detail.0.msg eq 'Value error, Invalid cron expression: not a cron' --save F05.cron-timezone/bad-cron
  control-automation api POST /v1 --json '{"name":"qa-cron","trigger":{"type":"cron","schedule":"0 0 31 2 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail.0.msg eq 'Value error, Cron expression cannot produce any future fire times: 0 0 31 2 *'
  control-automation api POST /v1 --json '{"name":"qa-cron","trigger":{"type":"cron","schedule":"0 9 * * 1","timezone":"Mars/Base"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail.0.msg eq 'Value error, Invalid timezone: Mars/Base'
  control-automation api POST /v1 --json '{"name":"qa-cron","trigger":{"schedule":"0 9 * * 1"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail.0.type eq union_tag_invalid --check detail.0.msg contains __missing_trigger_type__
  CRID=$(control-automation api POST /v1 --json '{"name":"qa-cron-paris","trigger":{"type":"cron","schedule":"0 9 * * 1","timezone":"Europe/Paris"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api GET "/v1/$CRID" --expect 200 --check trigger.timezone eq Europe/Paris --check trigger.schedule eq '0 9 * * 1'
  ```
- **Unknown fields and empty name (`F05.extra-fields`).** Bodies are `extra="forbid"`.
  ```sh
  control-automation api POST /v1 --json '{"name":"qa-extra","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","color":"red"}' --expect 422 --check detail.0.type eq extra_forbidden --check detail.0.loc.1 eq color --save F05.extra-fields/color
  control-automation api POST /v1 --json '{"name":"qa-extra","trigger":{"type":"cron","schedule":"0 0 1 1 *","every":"day"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api POST /v1 --json '{"name":"","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 422 --check detail.0.type eq string_too_short
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name = 'qa-extra'" --check rows.0.n eq 0
  ```
- **State and enabled on create (`F05.state-enabled`).** Two ways to start inactive; contradictions and DRAFT are refused.
  ```sh
  control-automation api POST /v1 --json '{"name":"qa-inactive","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","state":"INACTIVE"}' --expect 201 --check state eq INACTIVE --check enabled eq false --check disabled_reason eq null --save F05.state-enabled/inactive
  control-automation api POST /v1 --json '{"name":"qa-disabled","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","enabled":false}' --expect 201 --check state eq INACTIVE --check enabled eq false
  control-automation api POST /v1 --json '{"name":"qa-conflict","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","state":"ACTIVE","enabled":false}' --expect 422 --check detail.0.msg eq 'Value error, enabled must be true only when state is ACTIVE'
  control-automation api POST /v1 --json '{"name":"qa-draft","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","state":"DRAFT"}' --expect 422 --check detail.0.msg contains 'state=DRAFT is reserved for automation draft test artifacts'
  ```
- **Agent profile and model (`F05.profile-model`).** Mutually exclusive; local mode stores either as given.
  ```sh
  control-automation api POST /v1 --json '{"name":"qa-profile","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","agent_profile_id":"11111111-1111-4111-8111-111111111111","model":"qa-model"}' --expect 422 --check detail eq 'An agent profile already specifies the model' --save F05.profile-model/both
  control-automation api POST /v1 --json '{"name":"qa-profile","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","agent_profile_id":"11111111-1111-4111-8111-111111111111"}' --expect 201 --check agent_profile_id eq 11111111-1111-4111-8111-111111111111 --check model eq null
  control-automation api POST /v1 --json '{"name":"qa-model","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","model":"qa-model"}' --expect 201 --check model eq qa-model --check agent_profile_id eq null
  control-automation api POST /v1 --json '{"name":"qa-model","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","model":"-bad model"}' --expect 422 --check detail.0.type eq string_pattern_mismatch
  ```
- **keep_alive for continued conversations (`F05.keep-alive-continue`).** Forced on, and refusing to turn it off.
  ```sh
  KID=$(control-automation api POST /v1 --json '{"name":"qa-continue","trigger":{"type":"event","source":"qa-src","on":"qa.*","destination":"continue_conversation","subject_key_expr":"id"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --check keep_alive eq true --save F05.keep-alive-continue/forced --field id)
  control-automation api GET "/v1/$KID" --expect 200 --check keep_alive eq true --check trigger.destination eq continue_conversation
  control-automation api POST /v1 --json '{"name":"qa-continue-off","trigger":{"type":"event","source":"qa-src","on":"qa.*","destination":"continue_conversation","subject_key_expr":"id"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","keep_alive":false}' --expect 422 --check detail.0.msg contains "keep_alive cannot be false when destination is 'continue_conversation'"
  ```
  Presets do not apply this rule; see `F06.continue-keep-alive`.
- **Template idempotency (`F05.template-idempotent`).** The second create with the same template id returns the first automation unchanged.
  ```sh
  TPL="qa-tpl-$(date +%s%N)"
  T1=$(control-automation api POST /v1 --json "{\"name\":\"qa-template-1\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"https://qa.invalid/x.tgz\",\"entrypoint\":\"bash run.sh\",\"template\":{\"id\":\"$TPL\",\"version\":\"1\",\"config\":{\"k\":\"v\"}}}" --expect 201 --check preset_metadata.template.id eq "$TPL" --check preset_metadata.template.config.k eq v --save F05.template-idempotent/first --field id)
  control-automation api POST /v1 --json "{\"name\":\"qa-template-2\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 9 * * 1\"},\"tarball_path\":\"https://qa.invalid/y.tgz\",\"entrypoint\":\"bash other.sh\",\"template\":{\"id\":\"$TPL\",\"version\":\"2\"}}" --expect 200 --check id eq "$T1" --check name eq qa-template-1 --check preset_metadata.template.version eq 1 --check entrypoint eq 'bash run.sh' --save F05.template-idempotent/repeat
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name LIKE 'qa-template-%' AND deleted_at IS NULL" --check rows.0.n eq 1
  ```
  Only one row exists; the repeat's name, trigger and tarball were ignored.
- **List, page and filter (`F05.list`).** Newest first, stable paging, creator filter, bounds.
  ```sh
  L1=$(control-automation api POST /v1 --json '{"name":"qa-list-1","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  sleep 1.2
  L2=$(control-automation api POST /v1 --json '{"name":"qa-list-2","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  LT=$(control-automation api GET /v1 --expect 200 --check automations.0.id eq "$L2" --check automations.1.id eq "$L1" --quiet --field total)
  control-automation api GET /v1 --query limit=1 --query offset=1 --expect 200 --check automations len-eq 1 --check automations.0.id eq "$L1" --check total eq "$LT" --save F05.list/page
  control-automation api GET /v1 --query created_by=me --expect 200 --check total eq "$LT" --quiet
  control-automation api GET /v1 --query created_by=others --expect 200 --check total eq 0 --check automations len-eq 0
  control-automation api GET /v1 --query created_by=everyone --expect 422 --check detail.0.loc.1 eq created_by
  control-automation api GET /v1 --query limit=0 --expect 422
  control-automation api GET /v1 --query limit=101 --expect 422
  control-automation api GET /v1 --query offset=-1 --expect 422
  ```
  The `sleep` is deliberate: `created_at` has one-second precision, and
  rows in the same second are ordered by random UUID. `others` is empty
  because the local user is the only user.
- **Get errors (`F05.get`).** Unknown and malformed ids.
  ```sh
  control-automation api GET /v1/00000000-0000-4000-8000-000000000000 --expect 404 --check detail eq 'Automation not found' --save F05.get/unknown
  control-automation api GET /v1/not-a-uuid --expect 422 --check detail.0.loc.1 eq automation_id
  ```
- **PATCH fields (`F05.patch-fields`).** Partial update; untouched fields survive; GET agrees.
  ```sh
  PID=$(control-automation api POST /v1 --json '{"name":"qa-patch","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh","timeout":300}' --expect 201 --field id)
  U0=$(control-automation api GET "/v1/$PID" --expect 200 --field updated_at)
  control-automation api PATCH "/v1/$PID" --json '{"name":"qa-patch-renamed","trigger":{"type":"cron","schedule":"30 6 * * *","timezone":"Asia/Tokyo"},"timeout":900,"entrypoint":"bash run.sh --verbose","setup_script_path":"setup.sh"}' --expect 200 --check name eq qa-patch-renamed --save F05.patch-fields/patch
  control-automation api GET "/v1/$PID" --expect 200 --check name eq qa-patch-renamed --check trigger.schedule eq '30 6 * * *' --check trigger.timezone eq Asia/Tokyo --check timeout eq 900 --check entrypoint eq 'bash run.sh --verbose' --check setup_script_path eq setup.sh --check tarball_path eq https://qa.invalid/x.tgz --check state eq ACTIVE --check updated_at ne "$U0" --save F05.patch-fields/get
  control-automation api PATCH "/v1/$PID" --json '{}' --expect 200 --check name eq qa-patch-renamed --check timeout eq 900
  ```
  An empty body is a no-op 200.
- **PATCH state and enabled (`F05.patch-state`).** Disabling records why and when; re-enabling clears it.
  ```sh
  SID=$(control-automation api POST /v1 --json '{"name":"qa-state","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api PATCH "/v1/$SID" --json '{"state":"INACTIVE"}' --expect 200 --check state eq INACTIVE --check enabled eq false --save F05.patch-state/disable
  control-automation api GET "/v1/$SID" --expect 200 --check enabled eq false --check disabled_reason eq manual --check disabled_detail.reason eq manual --check disabled_detail.source eq user --check disabled_at ne null
  control-automation state sql "SELECT reason, source FROM automation_disable_events WHERE automation_id = replace('$SID','-','')" --check count eq 1 --check rows.0.reason eq manual --check rows.0.source eq manual
  control-automation api PATCH "/v1/$SID" --json '{"enabled":true}' --expect 200 --check state eq ACTIVE --save F05.patch-state/enable
  control-automation api GET "/v1/$SID" --expect 200 --check enabled eq true --check state eq ACTIVE --check disabled_reason eq null --check disabled_detail eq null --check disabled_at eq null
  control-automation api PATCH "/v1/$SID" --json '{"enabled":false}' --expect 200 --check state eq INACTIVE --check disabled_reason eq manual
  control-automation api PATCH "/v1/$SID" --json '{"state":"ACTIVE"}' --expect 200 --check enabled eq true --check disabled_reason eq null
  control-automation api PATCH "/v1/$SID" --json '{"state":"ACTIVE","enabled":false}' --expect 422
  ```
  `enabled` and `state` are two spellings of one switch (`enabled` is
  deprecated). Disabling also skips the automation's PENDING runs with
  `Automation disabled by user` (F07 drives that with a live run).
- **PATCH validation (`F05.patch-validation`).** Create rules apply to partial bodies; unknown ids are 404.
  ```sh
  VID=$(control-automation api POST /v1 --json '{"name":"qa-patch-val","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api PATCH "/v1/$VID" --json '{"timeout":0}' --expect 422 --check detail.0.msg eq 'Value error, timeout must be a positive number' --save F05.patch-validation/timeout
  control-automation api PATCH "/v1/$VID" --json '{"entrypoint":"/abs/run"}' --expect 422 --check detail.0.msg contains 'must be a relative path'
  control-automation api PATCH "/v1/$VID" --json '{"color":"red"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api PATCH "/v1/$VID" --json '{"state":"DRAFT"}' --expect 422 --check detail.0.msg contains 'state=DRAFT is reserved'
  control-automation api PATCH "/v1/$VID" --json '{"tarball_path":"ftp://qa.invalid/x"}' --expect 422
  control-automation api GET "/v1/$VID" --expect 200 --check timeout eq 600 --check entrypoint eq 'bash run.sh' --check state eq ACTIVE --check tarball_path eq https://qa.invalid/x.tgz
  control-automation api PATCH /v1/00000000-0000-4000-8000-000000000000 --json '{"name":"x"}' --expect 404 --check detail eq 'Automation not found'
  CCID=$(control-automation api POST /v1 --json '{"name":"qa-patch-continue","trigger":{"type":"event","source":"qa-src","on":"qa.*","destination":"continue_conversation","subject_key_expr":"id"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api PATCH "/v1/$CCID" --json '{"keep_alive":false}' --expect 400 --check detail contains "keep_alive cannot be false when destination is 'continue_conversation'" --save F05.patch-validation/keep-alive
  control-automation api GET "/v1/$CCID" --expect 200 --check keep_alive eq true
  ```
  The `keep_alive` rule is a 400 from the handler on PATCH, a 422 from the
  schema on create. PATCH does not re-check ownership of a new
  `tarball_path` (only the scheme regex applies).
- **Explicit null on required fields (`F05.patch-null`), known bug.** The schema accepts `null`, the database refuses it, and the client gets a 500.
  ```sh
  NID=$(control-automation api POST /v1 --json '{"name":"qa-null","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api PATCH "/v1/$NID" --json '{"name":null}' --expect 422 --save F05.patch-null/name  # bug: status eq 500
  ```
  Correct behavior is a 422 that names the field. Today `name`,
  `entrypoint`, `tarball_path`, `trigger` and `enabled` set to `null` each
  answer 500 `Internal Server Error` (an `IntegrityError: NOT NULL
  constraint failed` in the service log) and store nothing. `timeout: null`
  is stored as `null` (200) instead of falling back to the default, and
  `state: null` is ignored.
- **Soft delete (`F05.delete`).** 204, then every route answers 404 and the row keeps its tombstone.
  ```sh
  DAID=$(control-automation api POST /v1 --json '{"name":"qa-delete","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  DT0=$(control-automation api GET /v1 --query limit=1 --expect 200 --field total)
  control-automation api DELETE "/v1/$DAID" --expect 204 --save F05.delete/delete
  control-automation api GET "/v1/$DAID" --expect 404 --check detail eq 'Automation not found' --save F05.delete/get
  control-automation api PATCH "/v1/$DAID" --json '{"name":"x"}' --expect 404
  control-automation api DELETE "/v1/$DAID" --expect 404
  control-automation api GET /v1 --query limit=100 --expect 200 --check total eq $((DT0 - 1)) --check automations not-contains "$DAID" --quiet
  control-automation state sql "SELECT state, enabled, disabled_reason, deleted_at FROM automations WHERE id = replace('$DAID','-','')" --check rows.0.state eq INACTIVE --check rows.0.enabled eq 0 --check rows.0.disabled_reason eq manual_delete --check rows.0.deleted_at ne null
  ```
  Deleting an already inactive automation leaves `disabled_reason` as it
  was (no `manual_delete`). The underlying upload is not deleted.
- **Download an internal tarball (`F05.tarball-download`).** The uploaded bytes come back as an attachment.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  DLTP=$(control-automation api POST /v1/uploads --query name=qa-download-src --data-file "$TARF" --content-type application/gzip --expect 201 --field tarball_path)
  DLID=$(control-automation api POST /v1 --json "{\"name\":\"qa-download\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"$DLTP\",\"entrypoint\":\"bash run.sh\"}" --expect 201 --field id)
  control-automation api GET "/v1/$DLID/tarball" --expect 200 --quiet --check-header content-type eq application/x-tar --check-header content-disposition eq 'attachment; filename="qa-download.tar"' --raw-out "$AUTOMATION_VERIFY_RUN/qa-download.tar" --save F05.tarball-download/download
  cmp "$TARF" "$AUTOMATION_VERIFY_RUN/qa-download.tar"
  control-automation exec --expect-output run.sh -- tar tzf "$AUTOMATION_VERIFY_RUN/qa-download.tar"
  ```
  The body is the gzip archive exactly as uploaded, despite the
  `application/x-tar` type and the `.tar` filename.
- **Download filename sanitizing (`F05.tarball-filename`).** Non-ASCII names get an ASCII fallback and an encoded `filename*`.
  ```sh
  FTP=$(control-automation fixture tarball --kind echo --upload --field tarball_path)
  FNID=$(control-automation api POST /v1 --json "{\"name\":\"Café/qa \\\"rapport\\\"\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"$FTP\",\"entrypoint\":\"bash run.sh\"}" --expect 201 --field id)
  control-automation api GET "/v1/$FNID/tarball" --expect 200 --quiet --check-header content-disposition eq "attachment; filename=\"Cafeqa rapport.tar\"; filename*=UTF-8''Caf%C3%A9qa%20rapport.tar" --save F05.tarball-filename/non-ascii
  ```
  The slash and the quotes are dropped before either form is built.
- **External tarballs (`F05.tarball-external`).** `https://` redirects; bucket URLs cannot be proxied.
  ```sh
  XHID=$(control-automation api POST /v1 --json '{"name":"qa-redirect","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/qa.tar.gz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api GET "/v1/$XHID/tarball" --expect 302 --quiet --check-header location eq https://qa.invalid/qa.tar.gz --save F05.tarball-external/https
  XSID=$(control-automation api POST /v1 --json '{"name":"qa-s3","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"s3://qa-bucket/qa.tar.gz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api GET "/v1/$XSID/tarball" --expect 422 --check detail eq 'Cannot proxy s3:// tarball URLs. Retrieve the tarball_path from GET /api/automation/v1/{automation_id} and access the file directly.' --save F05.tarball-external/s3
  XGID=$(control-automation api POST /v1 --json '{"name":"qa-gs","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"gs://qa-bucket/qa.tar.gz","entrypoint":"bash run.sh"}' --expect 201 --field id)
  control-automation api GET "/v1/$XGID/tarball" --expect 422 --check detail contains 'Cannot proxy gs:// tarball URLs'
  control-automation api GET /v1/00000000-0000-4000-8000-000000000000/tarball --expect 404 --check detail eq 'Automation not found'
  ```
  The CLI does not follow redirects, so the 302 and its `Location` are
  visible. `{automation_id}` is literal text in the 422 message.
- **Key required (`F05.auth`).** No key or a wrong key gets 401 on every route.
  ```sh
  control-automation api GET /v1 --auth none --expect 401 --check detail contains 'Authentication required' --save F05.auth/list
  control-automation api GET /v1 --auth bad --expect 401 --check detail eq 'Invalid API key'
  control-automation api POST /v1 --auth none --json '{"name":"qa-noauth","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz","entrypoint":"bash run.sh"}' --expect 401
  control-automation api GET /v1/00000000-0000-4000-8000-000000000000 --auth none --expect 401
  control-automation api PATCH /v1/00000000-0000-4000-8000-000000000000 --auth bearer-bad --json '{"name":"x"}' --expect 401
  control-automation api DELETE /v1/00000000-0000-4000-8000-000000000000 --auth none --expect 401
  control-automation api GET /v1/00000000-0000-4000-8000-000000000000/tarball --auth none --expect 401
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name = 'qa-noauth'" --check rows.0.n eq 0
  ```

## Gotchas

- `POST /v1` checks the template before anything else except the
  profile/model pair: a repeat with an invalid tarball, model or trigger
  still answers 200 with the existing automation. The lookup is per org
  (not per user) and matches inactive and draft-state rows too; presets
  share it (`F06.template-shared`). Use a fresh `template.id` per replay.
- `http://` is accepted by the schema regex but refused by the handler;
  the schema's error message lists `http://` and omits `oh-internal://`.
  `https://`, `s3://` and `gs://` are never fetched at create time.
- `created_at` has one-second precision; ordering within a second is by
  random UUID.
- PATCH with `null` on a NOT NULL column is a 500 today (`F05.patch-null`);
  send only the fields you change, never `null` placeholders.
- `enabled` is deprecated in favor of `state` but still accepted on create
  and PATCH (with a server-side `DeprecationWarning`).
- In local mode the only user owns everything and holds both permissions,
  so the 403 paths (`Only admins, owners, or the automation creator can
  modify it`, `Only the automation creator can edit it; admins and owners
  can only turn it off or delete it`) and cloud model/profile lookups
  (`Model profile ... not found`, 502s) cannot be driven here.
- The tarball route serves preset tarballs too (F06); they are gzip
  archives served as `application/x-tar`, so use `tar tzf`, not `tar tf`.
- A deleted upload breaks the automation's download with 404
  `Tarball not found (underlying upload may have been deleted)`
  (`F04.delete-in-use`).

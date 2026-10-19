# Telemetry consent

The local-mode switch for product telemetry. Agent Canvas posts the user's
choice to `POST /v1/telemetry/consent` with its own PostHog distinct id; the
service stores one boolean per frontend id and answers the aggregate (any id
granted). Every later lifecycle event (`automation_created`,
`automation_deleted`, run events) is sent to PostHog only when the frontend
id it is attributed to has consented, or, with no id, when any id has.
Granting also sends `automation_telemetry_consent_granted`. In cloud mode
consent belongs to the main OpenHands app and the endpoint refuses.

Source: `openhands/automation/telemetry_router.py`, `openhands/automation/telemetry.py`, `openhands/automation/middleware.py`, `openhands/automation/schemas.py`

Launch: `--telemetry-sink`

Routes: `POST /api/automation/v1/telemetry/consent`

## Sub-features

- `F15.auth`: the consent route needs a key (401 without one).
- `F15.no-consent-no-capture`: before any consent, lifecycle events are not sent: creating an automation reaches PostHog with nothing.
- `F15.grant`: granting answers `{"consent_granted": true}`, stores it under the frontend id, and sends `automation_telemetry_consent_granted` (`consent_source:"agent_canvas"`) attributed to the body's id, not the header's.
- `F15.aggregate`: the answer is the aggregate: revoking another id still answers true; revoking the only granted id answers false; revoking sends no event.
- `F15.anonymous`: an omitted or blank `frontend_distinct_id` is stored as `__anonymous__`.
- `F15.capture-gating`: with consent, an event with no frontend id is sent under the backend id; an event attributed (by `X-OpenHands-Telemetry-Distinct-Id`) to a revoked id is dropped, and one attributed to a granted id is sent with that id and the client headers, without user identity.
- `F15.validation`: an unknown field, a missing `consent_granted` or a 257-character id answer 422.
- `F15.persist`: consent is stored in the database and survives a restart.
- `F15.cloud-mode`: in cloud mode the route answers 400 `Telemetry consent is managed by the main OpenHands app in cloud mode.`

## How to get to it (agent POV)

- REST: `POST /api/automation/v1/telemetry/consent` with
  `{"consent_granted": bool, "frontend_distinct_id": str | null}` (at most
  256 characters), answering `{"consent_granted": <aggregate>}`. There is no
  GET; the stored map is the service metadata row
  `posthog_frontend_consent_by_distinct_id` (`state sql`).
- Headers read on every request: `X-OpenHands-Telemetry-Distinct-Id`,
  `X-OpenHands-Client`, `X-OpenHands-Client-Version` (stripped, cut to 256).
  Local mode trusts the distinct id; cloud mode drops it.
- Agent Canvas calls the consent route when the user answers the analytics
  prompt and sends the headers on its API calls.
- Config: `AUTOMATION_POSTHOG_API_KEY` (empty disables sending; the consent
  route still stores), `AUTOMATION_POSTHOG_HOST`.
  `launch --telemetry-sink` sets the key to `phc_qa_verify` and the host to a
  local HTTP sink named `telemetry`, which records each `POST /capture/`;
  `sink read telemetry` shows what was sent.

## Driving it with control-automation

Preconditions:

- A fresh run launched with `--telemetry-sink` (the family's `Launch:`
  line), so nothing has been consented and the sink is empty. No LLM.
- Bullets run in order: the consent map each one leaves is the next one's
  starting point (noted per bullet).

- **Key required (`F15.auth`).** No key, no write.
  ```sh
  control-automation api POST /v1/telemetry/consent --auth none --json '{"consent_granted":true,"frontend_distinct_id":"qa-fe-a"}' --expect 401 --check detail matches '^Authentication required' --save F15.auth/no-key
  control-automation state sql "SELECT count(*) AS n FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --check rows.0.n eq 0
  ```
- **No consent, nothing sent (`F15.no-consent-no-capture`).** Create an automation on the fresh run; capture is awaited inside the request, so the sink is final when the call returns.
  ```sh
  TAID=$(control-automation fixture automation --kind callback --name qa-tel-before --field id)
  control-automation api GET "/v1/$TAID" --expect 200 --check name eq qa-tel-before --quiet
  control-automation sink read telemetry --expect-max 0 --save F15.no-consent-no-capture/sink
  ```
- **Grant (`F15.grant`).** Grant for `qa-fe-a` while the header names another id; the stored map and the one event sent both use the body's id.
  ```sh
  control-automation api POST /v1/telemetry/consent --header 'X-OpenHands-Telemetry-Distinct-Id: qa-hdr' --json '{"consent_granted":true,"frontend_distinct_id":"qa-fe-a"}' --expect 200 --check consent_granted eq true --save F15.grant/response
  control-automation state sql "SELECT value FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --raw --check rows.0.value eq '{"qa-fe-a": true}'
  control-automation sink read telemetry --path /capture/ --expect-min 1 --expect-max 1 --save F15.grant/sink
  control-automation sink read telemetry --contains '"event": "automation_telemetry_consent_granted", "distinct_id": "qa-fe-a"' --expect-min 1
  control-automation sink read telemetry --contains '"consent_source": "agent_canvas"' --expect-min 1
  control-automation sink read telemetry --contains qa-hdr --expect-max 0
  ```
  Leaves `{"qa-fe-a": true}`.
- **Aggregate answer (`F15.aggregate`).** Revoking `qa-fe-b` still answers true because `qa-fe-a` is granted; revoking `qa-fe-a` answers false. Revocations send nothing.
  Requires: `F15.grant`
  ```sh
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":false,"frontend_distinct_id":"qa-fe-b"}' --expect 200 --check consent_granted eq true --save F15.aggregate/other-id
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":false,"frontend_distinct_id":"qa-fe-a"}' --expect 200 --check consent_granted eq false --save F15.aggregate/revoked
  control-automation state sql "SELECT value FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --raw --check rows.0.value eq '{"qa-fe-a": false, "qa-fe-b": false}'
  control-automation sink read telemetry --expect-max 1
  ```
  Leaves both ids revoked.
- **Anonymous id (`F15.anonymous`).** No id, then a blank id, land on the same `__anonymous__` entry.
  Requires: `F15.aggregate`
  ```sh
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":true}' --expect 200 --check consent_granted eq true --save F15.anonymous/granted
  control-automation state sql "SELECT value FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --raw --check rows.0.value eq '{"__anonymous__": true, "qa-fe-a": false, "qa-fe-b": false}'
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":false,"frontend_distinct_id":"   "}' --expect 200 --check consent_granted eq false
  control-automation state sql "SELECT value FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --raw --check rows.0.value eq '{"__anonymous__": false, "qa-fe-a": false, "qa-fe-b": false}'
  ```
  Leaves every id revoked.
- **Capture follows the attributed id (`F15.capture-gating`).** Grant `qa-fe-a` again, then: a creation with no id header is sent under the backend id; a delete attributed to revoked `qa-fe-b` is dropped; a delete attributed to `qa-fe-a` is sent with its id and client headers, and no email or key ever reaches the sink.
  Requires: `F15.no-consent-no-capture`, `F15.anonymous`
  ```sh
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":true,"frontend_distinct_id":"qa-fe-a"}' --expect 200 --check consent_granted eq true
  GAID=$(control-automation fixture automation --kind callback --name qa-tel-after --field id)
  control-automation sink read telemetry --contains '"event": "automation_created", "distinct_id": "automation-backend:' --expect-min 1 --expect-max 1 --save F15.capture-gating/created
  control-automation api DELETE "/v1/$TAID" --header 'X-OpenHands-Telemetry-Distinct-Id: qa-fe-b' --expect 204
  control-automation sink read telemetry --contains automation_deleted --expect-max 0
  control-automation api DELETE "/v1/$GAID" --header 'X-OpenHands-Telemetry-Distinct-Id: qa-fe-a' --header 'X-OpenHands-Client: qa-canvas' --header 'X-OpenHands-Client-Version: 9.9.9' --expect 204
  control-automation sink read telemetry --contains '"event": "automation_deleted", "distinct_id": "qa-fe-a"' --expect-min 1 --expect-max 1 --save F15.capture-gating/deleted
  control-automation sink read telemetry --contains '"client_source": "qa-canvas", "client_version": "9.9.9"' --expect-min 1
  control-automation sink read telemetry --contains '"deployment_mode": "local"' --expect-min 3
  control-automation sink read telemetry --contains local@localhost --expect-max 0
  control-automation sink read telemetry --contains cloud_user_id --expect-max 0
  control-automation api GET "/v1/$GAID" --expect 404
  ```
  Leaves `qa-fe-a` granted.
- **Body validation (`F15.validation`).** Strict body: unknown fields, a missing flag and an over-long id are refused, and the stored map does not change.
  Requires: `F15.capture-gating`
  ```sh
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":true,"extra":1}' --expect 422 --check detail.0.type eq extra_forbidden --save F15.validation/extra
  control-automation api POST /v1/telemetry/consent --json '{"frontend_distinct_id":"qa-fe-x"}' --expect 422 --check detail.0.loc contains consent_granted --check detail.0.type eq missing
  control-automation api POST /v1/telemetry/consent --json "{\"consent_granted\":true,\"frontend_distinct_id\":\"$(printf 'x%.0s' $(seq 257))\"}" --expect 422 --check detail.0.type eq string_too_long
  control-automation state sql "SELECT value FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --raw --check rows.0.value eq '{"__anonymous__": false, "qa-fe-a": true, "qa-fe-b": false}'
  ```
- **Survives a restart (`F15.persist`).** After restarting the service, revoking an unrelated id still answers true: `qa-fe-a`'s grant came back from the database.
  Requires: `F15.capture-gating`
  ```sh
  control-automation restart --service automation
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":false,"frontend_distinct_id":"qa-fe-z"}' --expect 200 --check consent_granted eq true --save F15.persist/after-restart
  control-automation state sql "SELECT value FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --raw --check rows.0.value contains '"qa-fe-a": true'
  ```
- **Cloud mode refuses (`F15.cloud-mode`).** Empty `AUTOMATION_AGENT_SERVER_URL` turns local mode off; a users/me stub answers auth for the run's key. The refusal comes after auth. Restore afterwards.
  ```sh
  UM=$(control-automation fixture http-sink --name usersme --body '{"id":"11111111-1111-4111-8111-111111111111","org_id":"22222222-2222-4222-8222-222222222222","email":"qa-other@example.test","role":"member","permissions":["view_automations","manage_automations"]}' --field url)
  control-automation restart --service automation --env AUTOMATION_AGENT_SERVER_URL= --env "AUTOMATION_OPENHANDS_API_BASE_URL=$UM"
  control-automation api POST /v1/telemetry/consent --json '{"consent_granted":true,"frontend_distinct_id":"qa-fe-cloud"}' --expect 400 --check detail eq 'Telemetry consent is managed by the main OpenHands app in cloud mode.' --save F15.cloud-mode/refused
  control-automation api POST /v1/telemetry/consent --auth none --json '{"consent_granted":true}' --expect 401
  control-automation restart --service automation --reset-env
  control-automation state sql "SELECT value FROM automation_service_metadata WHERE key = 'posthog_frontend_consent_by_distinct_id'" --raw --check rows.0.value not-contains qa-fe-cloud
  ```

## Gotchas

- The response is the aggregate over all ids, not the value just sent:
  `{"consent_granted": false, ...}` can answer true.
- Capture is gated per attributed id: the `X-OpenHands-Telemetry-Distinct-Id`
  header first (for runs, the id stored on the run or automation at
  creation), and only when there is no id the aggregate. A grant for one
  browser does not let events attributed to another browser through.
- The consent event uses the body's `frontend_distinct_id`, not the header.
- Without `--telemetry-sink` the run sets `AUTOMATION_POSTHOG_API_KEY=""`, so
  nothing is ever sent and there is no sink to read; the route still stores
  consent.
- Sending is awaited inside the request (2 s timeout) and failures are
  swallowed, so a slow or broken PostHog host slows requests but never fails
  them.
- `consent_granted` is a lax Pydantic bool: `"yes"`, `"true"` and `1` are
  accepted as true (seen live with `"yes"`).
- The sink records the request's `api_key` field (`phc_qa_verify`, a fake
  key); in local mode no user email, key or `cloud_*` id is sent, only the
  frontend id, the backend id (`automation-backend:<uuid>`, created on first
  capture and stored in `automation_service_metadata`), versions and the
  automation's id, trigger type and timeout.
- `sink read --contains` matches one substring of the recorded JSON (keys in
  payload order, `", "` and `": "` separators); it cannot check fields.

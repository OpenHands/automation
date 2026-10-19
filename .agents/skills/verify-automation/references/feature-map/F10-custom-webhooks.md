# Custom webhooks

How an organization registers an external service (Stripe, a CI system, an
in-house tool) as an event source: create a webhook for a `source` name, get
back a signing secret and the URL to paste into the external service, then
list, read, reconfigure, disable, rotate the secret of, or delete it. The
secret is shown exactly once (on create when the server generated it, and on
rotate) and never appears in a list or a GET. What the events posted to the
`webhook_url` do is F11; this family proves the configuration surface and the
two places it changes ingestion: a disabled or deleted webhook stops
deliveries, and a rotated secret invalidates the old one at once.

Source: `openhands/automation/webhook_router.py`, `openhands/automation/schemas.py`, `openhands/automation/models.py`, `openhands/automation/providers.py`, `openhands/automation/utils/webhook.py`

Routes: `POST /api/automation/v1/webhooks`, `GET /api/automation/v1/webhooks`,
`GET /api/automation/v1/webhooks/{webhook_id}`,
`PATCH /api/automation/v1/webhooks/{webhook_id}`,
`DELETE /api/automation/v1/webhooks/{webhook_id}`,
`POST /api/automation/v1/webhooks/{webhook_id}/rotate-secret`

## Sub-features

- `F10.create-generated`: `POST /v1/webhooks` without `webhook_secret` answers 201 with a generated `whsec_` secret (49 chars) and the defaults `event_key_expr: type`, `signature_header: X-Signature-256`, `signature_scheme: hmac_sha256_hex`, `event_id_header: null`, `enabled: true`; a GET reads the same row back without the secret.
- `F10.create-supplied`: a supplied `webhook_secret` is stored but not echoed (`webhook_secret: null`), the `source` is lowercased, and a delivery signed with the supplied secret is accepted.
- `F10.webhook-url`: `webhook_url` is `<AUTOMATION_BASE_URL>/api/automation/v1/events/<org_id>/<source>` with the local org, and that URL is the live ingestion route.
- `F10.validation`: reserved built-in names, a bad `source`, an unknown `signature_scheme`, an invalid JMESPath `event_key_expr`, bad header names, a short secret, an unknown field and an empty name each answer 422 with the field's message, a missing key answers 401, and none of them stores a row.
- `F10.duplicate`: a second webhook for the same source in the org (in any letter case) answers 409 `Webhook source '<source>' already exists for this org`.
- `F10.list`: `GET /v1/webhooks` pages newest first with `limit`/`offset`, reports `total`, never includes `webhook_secret`, and rejects `limit=101` and `offset=-1` with 422.
- `F10.get-missing`: GET, PATCH, DELETE and rotate on an unknown id answer 404 `Webhook not found`; a non-UUID id answers 422.
- `F10.patch`: PATCH changes `name`, `event_key_expr`, `signature_header` and `event_id_header` (set, then cleared with `null`), bumps `updated_at`, and a GET reads the change back; `signature_scheme: null` stores NULL and reads as `hmac_sha256_hex`; `{}` changes nothing; `source` and `webhook_secret` are refused with 422.
- `F10.patch-null-required`: an explicit `null` for a NOT NULL field (`name`, `enabled`) should be refused with 422 but answers 500 (known bug).
- `F10.disable`: `enabled: false` makes deliveries answer 404 `Unknown webhook source`, and re-enabling restores them.
- `F10.rotate`: rotate-secret answers 200 with a new `whsec_` secret; a delivery signed with the old secret answers 401 and one signed with the new secret 200; GET still shows no secret.
- `F10.delete`: DELETE answers 204 with an empty body; the webhook is gone from GET and the list, deliveries answer 404, and a second DELETE answers 404.

## How to get to it (agent POV)

- REST: the six routes above, all behind the local API key
  (`X-Session-API-Key` or `Authorization: Bearer`); create, PATCH, DELETE and
  rotate need `manage_automations`, the reads `view_automations` (the local
  user has both). Every recipe here drives REST with the `api` verb;
  deliveries use the `event send` verb (F11).
- The external service is configured with `webhook_url` and the secret;
  Agent Canvas shows the secret once after create or rotate.
- Config: `AUTOMATION_BASE_URL` (default `http://localhost:<AUTOMATION_SERVER_PORT>`)
  builds `webhook_url`; launch sets it to the service's own URL. No secret
  env var is involved (that is the built-ins' `AUTOMATION_WEBHOOK_SECRET`, F11).
- There is no SDK or TypeScript client method for these routes.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM, no network.
- Webhook names and sources are prefixed `qa-`. Generated secrets are captured
  into shell variables with `--field webhook_secret --unsafe-unredacted`;
  everywhere else (body, `--save`, check output) the CLI redacts `whsec_`
  values and every `event send --secret`.

- **Create with a generated secret (`F10.create-generated`).** Defaults filled in, secret returned once, GET without it.
  ```sh
  GEN_SECRET=$(control-automation api POST /v1/webhooks --json '{"name":"qa generated","source":"qa-gen"}' --expect 201 --field webhook_secret --unsafe-unredacted)
  test "${GEN_SECRET#whsec_}" != "$GEN_SECRET" && test "${#GEN_SECRET}" -eq 49
  GEN_ID=$(control-automation api GET /v1/webhooks --expect 200 --field webhooks.0.id)
  control-automation api GET "/v1/webhooks/$GEN_ID" --expect 200 --check source eq qa-gen --check name eq 'qa generated' --check event_key_expr eq type --check signature_header eq X-Signature-256 --check signature_scheme eq hmac_sha256_hex --check event_id_header eq null --check enabled eq true --check webhook_secret missing --save F10.create-generated/get
  ```
  The secret starts with `whsec_` and is 49 characters; the GET has every
  default and no `webhook_secret` key.
- **Create with a supplied secret (`F10.create-supplied`).** Not echoed, source lowercased, and the stored secret verifies deliveries.
  ```sh
  OWN_ID=$(control-automation api POST /v1/webhooks --json '{"name":"qa own","source":"QA-Own","webhook_secret":"qa-own-secret-1"}' --expect 201 --check webhook_secret eq null --check source eq qa-own --save F10.create-supplied/create --field id)
  control-automation api GET "/v1/webhooks/$OWN_ID" --expect 200 --check source eq qa-own
  control-automation event send qa-own --secret qa-own-secret-1 --json '{"type":"qa.ping"}' --expect 200 --check received eq true
  ```
  `webhook_secret` is `null` in the 201 body, the source reads back as
  `qa-own`, and a delivery signed with `qa-own-secret-1` is accepted.
- **Webhook URL (`F10.webhook-url`).** The URL is built from the service's base URL and the local org, and it is the live ingestion route.
  ```sh
  HOOK_URL=$(control-automation api POST /v1/webhooks --json '{"name":"qa url","source":"qa-url","webhook_secret":"qa-url-secret"}' --expect 201 --check org_id eq 46d09c5c-7d7d-585f-8c37-a6b45a3de738 --check webhook_url matches '/api/automation/v1/events/46d09c5c-7d7d-585f-8c37-a6b45a3de738/qa-url$' --save F10.webhook-url/create --field webhook_url)
  control-automation exec --expect-output "$HOOK_URL" -- sh -c 'echo "$AUTOMATION_API_URL/v1/events/46d09c5c-7d7d-585f-8c37-a6b45a3de738/qa-url"'
  control-automation exec --expect-output 401 -- sh -c "curl -s -o /dev/null -w '%{http_code}' -X POST -d '{}' '$HOOK_URL'"
  ```
  The URL equals the run's API base plus `/v1/events/<org>/qa-url`, and an
  unsigned POST to it reaches the event route (401 for the missing
  signature, not 404).
- **Validation (`F10.validation`).** Each bad field is named in a 422, and nothing is stored.
  ```sh
  N0=$(control-automation api GET /v1/webhooks --expect 200 --field total)
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"github"}' --expect 422 --check detail.0.msg contains "'github' is a reserved source name" --save F10.validation/reserved
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"GitHub"}' --expect 422 --check detail.0.msg contains 'reserved source name'
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"jira_dc"}' --expect 422 --check detail.0.msg contains 'reserved source name'
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"bitbucket_data_center"}' --expect 422 --check detail.0.msg contains 'reserved source name'
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa_bad"}' --expect 422 --check detail.0.loc contains source --check detail.0.msg contains 'Source must be lowercase alphanumeric with hyphens'
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"-qa"}' --expect 422 --check detail.0.msg contains 'Source must be lowercase alphanumeric'
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa-v","signature_scheme":"md5"}' --expect 422 --check detail.0.msg contains "Invalid signature_scheme 'md5'"
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa-v","event_key_expr":"a[["}' --expect 422 --check detail.0.loc contains event_key_expr --check detail.0.msg contains 'Invalid JMESPath expression'
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa-v","signature_header":"X_Sig"}' --expect 422 --check detail.0.loc contains signature_header --check detail.0.msg contains 'Header must be alphanumeric with hyphens'
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa-v","event_id_header":"1abc"}' --expect 422 --check detail.0.loc contains event_id_header
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa-v","webhook_secret":"short"}' --expect 422 --check detail.0.type eq string_too_short
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa-v","bogus":1}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api POST /v1/webhooks --json '{"name":"","source":"qa-v"}' --expect 422 --check detail.0.loc contains name
  control-automation api POST /v1/webhooks --json '{"name":"qa","source":"qa-v"}' --auth none --expect 401
  control-automation api GET /v1/webhooks --expect 200 --check total eq "$N0"
  ```
  Every refusal is a 422 naming the field (401 without a key), and the list
  `total` is unchanged afterwards.
- **Duplicate source (`F10.duplicate`).** One webhook per source per org, case-insensitively.
  ```sh
  control-automation api POST /v1/webhooks --json '{"name":"qa dup","source":"qa-dup"}' --expect 201 --quiet
  control-automation api POST /v1/webhooks --json '{"name":"qa dup 2","source":"qa-dup"}' --expect 409 --check detail eq "Webhook source 'qa-dup' already exists for this org" --save F10.duplicate/conflict
  control-automation api POST /v1/webhooks --json '{"name":"qa dup 3","source":"QA-DUP"}' --expect 409 --check detail contains "'qa-dup'"
  ```
  The `--quiet` create hides the generated secret it returns.
- **List (`F10.list`).** Newest first, paged, never a secret.
  ```sh
  control-automation api POST /v1/webhooks --json '{"name":"qa list a","source":"qa-list-a","webhook_secret":"qa-list-secret"}' --expect 201 --quiet
  sleep 1.1
  control-automation api POST /v1/webhooks --json '{"name":"qa list b","source":"qa-list-b","webhook_secret":"qa-list-secret"}' --expect 201 --quiet
  TOTAL=$(control-automation api GET /v1/webhooks --expect 200 --field total)
  test "$TOTAL" -ge 2
  control-automation api GET /v1/webhooks --query limit=2 --expect 200 --check webhooks len-eq 2 --check webhooks.0.source eq qa-list-b --check webhooks.1.source eq qa-list-a --check webhooks.0.webhook_secret missing --check total eq "$TOTAL" --save F10.list/page1
  control-automation api GET /v1/webhooks --query limit=1 --query offset=1 --expect 200 --check webhooks len-eq 1 --check webhooks.0.source eq qa-list-a
  control-automation api GET /v1/webhooks --query limit=100 --expect 200 --check webhooks len-eq "$TOTAL"
  control-automation api GET /v1/webhooks --query limit=101 --expect 422 --check detail.0.loc contains limit
  control-automation api GET /v1/webhooks --query offset=-1 --expect 422 --check detail.0.loc contains offset
  ```
  `qa-list-b` (created last) comes first, `offset=1` skips it, and no row has
  a `webhook_secret` key. The `sleep 1.1` is deliberate: `created_at` has
  one-second resolution and the list has no tiebreaker (see Gotchas).
- **Unknown and malformed ids (`F10.get-missing`).** Every id route answers 404 for an unknown UUID and 422 for a non-UUID.
  ```sh
  NOPE=00000000-0000-0000-0000-000000000000
  control-automation api GET "/v1/webhooks/$NOPE" --expect 404 --check detail eq 'Webhook not found' --save F10.get-missing/get
  control-automation api PATCH "/v1/webhooks/$NOPE" --json '{"name":"qa"}' --expect 404 --check detail eq 'Webhook not found'
  control-automation api DELETE "/v1/webhooks/$NOPE" --expect 404 --check detail eq 'Webhook not found'
  control-automation api POST "/v1/webhooks/$NOPE/rotate-secret" --expect 404 --check detail eq 'Webhook not found'
  control-automation api GET /v1/webhooks/not-a-uuid --expect 422 --check detail.0.loc contains webhook_id
  ```
- **Update (`F10.patch`).** Each field changes, reads back through GET, and the two immutable fields are refused.
  ```sh
  P_ID=$(control-automation api POST /v1/webhooks --json '{"name":"qa patch","source":"qa-patch","webhook_secret":"qa-patch-secret"}' --expect 201 --field id)
  U0=$(control-automation api GET "/v1/webhooks/$P_ID" --expect 200 --field updated_at)
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{"name":"qa patched","event_key_expr":"event.kind","signature_header":"X-Qa-Sig","event_id_header":"X-Qa-Delivery"}' --expect 200 --check name eq 'qa patched' --save F10.patch/patch
  control-automation api GET "/v1/webhooks/$P_ID" --expect 200 --check name eq 'qa patched' --check event_key_expr eq event.kind --check signature_header eq X-Qa-Sig --check event_id_header eq X-Qa-Delivery --check updated_at ne "$U0" --check source eq qa-patch --save F10.patch/get
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{"event_id_header":null}' --expect 200 --check event_id_header eq null
  control-automation api GET "/v1/webhooks/$P_ID" --expect 200 --check event_id_header eq null --check signature_header eq X-Qa-Sig
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{"signature_scheme":"slack_v0"}' --expect 200 --check signature_scheme eq slack_v0
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{"signature_scheme":null}' --expect 200 --check signature_scheme eq hmac_sha256_hex
  control-automation state sql "SELECT signature_scheme FROM custom_webhooks WHERE source = 'qa-patch'" --check rows.0.signature_scheme eq null
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{}' --expect 200 --check name eq 'qa patched' --check event_key_expr eq event.kind
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{"source":"qa-other"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{"webhook_secret":"qa-new-secret"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api PATCH "/v1/webhooks/$P_ID" --json '{"signature_scheme":"md5"}' --expect 422 --check detail.0.msg contains 'Invalid signature_scheme'
  control-automation api GET "/v1/webhooks/$P_ID" --expect 200 --check source eq qa-patch --check signature_scheme eq hmac_sha256_hex
  ```
  The GET after the first PATCH shows all four changes and a new
  `updated_at`; `event_id_header: null` clears it; a NULL
  `signature_scheme` is stored as SQL NULL and served as `hmac_sha256_hex`;
  the refused PATCHes leave the row as it was.
- **Explicit null on a required field (`F10.patch-null-required`), known bug.** The update model accepts `null` for every field, the handler writes it, and the NOT NULL constraint fails at commit with no handler, so the client gets a bare 500 instead of a 422. The row is left unchanged.
  ```sh
  NUL_ID=$(control-automation api POST /v1/webhooks --json '{"name":"qa null","source":"qa-null","webhook_secret":"qa-null-secret"}' --expect 201 --field id)
  control-automation api PATCH "/v1/webhooks/$NUL_ID" --json '{"name":null}' --expect 422 --save F10.patch-null-required/name-null  # bug
  ```
  Expected: 422 naming `name` (and the same for `enabled`, `event_key_expr`,
  `signature_header`). Actual: `500 Internal Server Error` with an
  `IntegrityError` traceback in the service log; a GET afterwards still shows
  `name: qa null`.
- **Disable stops deliveries (`F10.disable`).** A disabled webhook is unknown to the event route until re-enabled.
  ```sh
  D_ID=$(control-automation api POST /v1/webhooks --json '{"name":"qa disable","source":"qa-disable","webhook_secret":"qa-disable-secret"}' --expect 201 --field id)
  control-automation event send qa-disable --secret qa-disable-secret --json '{"type":"qa.ping"}' --expect 200
  control-automation api PATCH "/v1/webhooks/$D_ID" --json '{"enabled":false}' --expect 200 --check enabled eq false
  control-automation api GET "/v1/webhooks/$D_ID" --expect 200 --check enabled eq false
  control-automation event send qa-disable --secret qa-disable-secret --json '{"type":"qa.ping"}' --expect 404 --check detail eq 'Unknown webhook source: qa-disable' --save F10.disable/delivery-refused
  control-automation api PATCH "/v1/webhooks/$D_ID" --json '{"enabled":true}' --expect 200 --check enabled eq true
  control-automation event send qa-disable --secret qa-disable-secret --json '{"type":"qa.ping"}' --expect 200
  ```
  The same signed delivery answers 200, then 404 while disabled, then 200
  again.
- **Rotate the secret (`F10.rotate`).** The new secret works at once and the old one stops working at once.
  ```sh
  R_ID=$(control-automation api POST /v1/webhooks --json '{"name":"qa rotate","source":"qa-rotate"}' --expect 201 --field id)
  R_OLD=$(control-automation api POST "/v1/webhooks/$R_ID/rotate-secret" --expect 200 --field webhook_secret --unsafe-unredacted)
  control-automation event send qa-rotate --secret "$R_OLD" --json '{"type":"qa.ping"}' --expect 200
  R_NEW=$(control-automation api POST "/v1/webhooks/$R_ID/rotate-secret" --expect 200 --field webhook_secret --unsafe-unredacted)
  test "${R_NEW#whsec_}" != "$R_NEW" && test "$R_NEW" != "$R_OLD"
  control-automation event send qa-rotate --secret "$R_OLD" --json '{"type":"qa.ping"}' --expect 401 --check detail eq 'Invalid signature' --save F10.rotate/old-secret
  control-automation event send qa-rotate --secret "$R_NEW" --json '{"type":"qa.ping"}' --expect 200 --check received eq true --save F10.rotate/new-secret
  control-automation api GET "/v1/webhooks/$R_ID" --expect 200 --check webhook_secret missing
  ```
  The first rotate turns the create-time secret (never captured here) into
  `R_OLD`, which signs a good delivery; after the second rotate `R_OLD` gets
  401 `Invalid signature` and `R_NEW` gets 200.
- **Delete (`F10.delete`).** 204, then gone everywhere.
  ```sh
  X_ID=$(control-automation api POST /v1/webhooks --json '{"name":"qa delete","source":"qa-delete","webhook_secret":"qa-delete-secret"}' --expect 201 --field id)
  XN=$(control-automation api GET /v1/webhooks --query limit=100 --expect 200 --field total)
  control-automation event send qa-delete --secret qa-delete-secret --json '{"type":"qa.ping"}' --expect 200
  control-automation api DELETE "/v1/webhooks/$X_ID" --expect 204 --save F10.delete/delete
  control-automation api GET "/v1/webhooks/$X_ID" --expect 404 --check detail eq 'Webhook not found'
  control-automation api GET /v1/webhooks --query limit=100 --expect 200 --check total eq "$((XN - 1))"
  control-automation event send qa-delete --secret qa-delete-secret --json '{"type":"qa.ping"}' --expect 404 --check detail eq 'Unknown webhook source: qa-delete'
  control-automation api DELETE "/v1/webhooks/$X_ID" --expect 404
  ```
  The source name is free again after the delete (a new create for
  `qa-delete` would succeed).

## Gotchas

- The secret is shown only in the create response (and only when the server
  generated it) and in the rotate response. A supplied secret cannot be read
  back and cannot be set again later: rotate always generates one. It is stored
  in plaintext in `custom_webhooks.webhook_secret`.
- `control-automation` redacts generated secrets by shape (`whsec_...`) and
  any secret passed to `event send`; `--field webhook_secret` needs
  `--unsafe-unredacted` to hand the clear value to the capturing shell.
- `source` is lowercased before validation and before the uniqueness check,
  so `QA-DUP` collides with `qa-dup`, and the 409 names the lowercased form.
  `github`, `jira_dc` and `bitbucket_data_center` are reserved; `slack` is not
  (F17 uses that).
- `source` cannot be changed by PATCH; delete and recreate instead (the new
  webhook gets a new secret and the external service must be reconfigured).
- PATCH applies `exclude_unset`: an omitted field is untouched, an explicit
  `null` is written. That clears the nullable `event_id_header` and
  `signature_scheme` (served as `hmac_sha256_hex`), but for `name`,
  `enabled`, `event_key_expr` and `signature_header` it is the 500 of
  `F10.patch-null-required`.
- The list orders by `created_at` only, stored with one-second resolution on
  SQLite and with no tiebreaker, so webhooks created in the same second can
  come back in either order and can shift between pages.
- `updated_at` comes back with microseconds after a write and without them in
  the create response; compare values, not formats.
- A disabled webhook and an unknown source give the same 404 on the event
  route, so the sender cannot tell them apart.
- `webhook_url` follows `AUTOMATION_BASE_URL`: behind a proxy or with a base
  path (F01) the URL handed out changes with it.

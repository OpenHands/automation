# Authentication and CORS

How a caller proves who it is, and which browser origins may call the
service. In local mode (the mode `launch` runs) the service accepts one
configured key, sent as `X-Session-API-Key` or `Authorization: Bearer`, and
maps it to a fixed local user and organization. Anything else (a session
cookie, or any key when no local key is configured) is checked against the
OpenHands app's `GET /api/v1/users/me`, which decides the user, the
organization and the permissions. The probes and the signed event-ingestion
routes need no key. CORS is two policies: requests that carry an API-key
header get `Access-Control-Allow-Origin: *` without credentials, everything
else is held to the `AUTOMATION_CORS_ORIGINS` allowlist with credentials.

Source: `openhands/automation/auth.py`, `openhands/automation/middleware.py`, `openhands/automation/app.py`, `openhands/automation/config.py`

Routes: none

## Sub-features

- `F02.missing-key`: a protected route without any credential answers 401 `Authentication required: provide Bearer token, X-Session-API-Key header, or session cookie`; an empty `X-Session-API-Key` counts as missing.
- `F02.session-key`: the local key in `X-Session-API-Key` gets in; a wrong key answers 401 `Invalid API key`.
- `F02.bearer`: the local key as `Authorization: Bearer` gets in; a wrong Bearer key answers 401 `Invalid API key`; any other scheme (`Token`, lowercase `bearer`) is ignored.
- `F02.bearer-precedence`: when both headers are sent, Bearer wins: a wrong Bearer key with a correct session key answers 401.
- `F02.empty-bearer`: a Bearer value that is blank after stripping answers 401 `Empty API key`; a bare `Bearer ` is trimmed by HTTP and reads as no credential.
- `F02.rotated-key`: after `restart --rotate-key` the previous key answers 401 `Invalid API key`, the new one gets in, and stored automations survive.
- `F02.local-identity`: everything created with the local key carries the fixed local `user_id` and `org_id`; `X-Org-Id` is not read on this path.
- `F02.no-key-routes`: probes and event ingestion need no key: an unknown event source answers 404, an unsigned delivery 401 about the signature header, a signed one 200, never the session-key 401.
- `F02.cookie-upstream`: in local mode a cookie-only request is not accepted locally; it is forwarded to `{AUTOMATION_OPENHANDS_API_BASE_URL}/api/v1/users/me` and that answer decides the user.
- `F02.org-scoping`: a user of another organization sees none of the local organization's automations and gets 404 for them.
- `F02.permissions`: a user without `manage_automations` gets 403 `Requires manage_automations permission` on a manage-only route, and nothing is deleted.
- `F02.org-header`: on the upstream path a non-UUID `X-Org-Id` answers 400 `Invalid X-Org-Id header (must be a UUID)`; a valid one is forwarded upstream.
- `F02.auth-cache`: a wrong local key never calls upstream, and a successful upstream check is cached, so repeated cookie requests reach upstream once.
- `F02.upstream-errors`: upstream 401 answers 401 `Invalid or expired session cookie`; an unreachable upstream answers 502 `Failed to reach OpenHands API for authentication`.
- `F02.cors-allowlist`: a preflight from an allowlisted origin (`http://localhost:3001`, `http://127.0.0.1:3001`) gets that origin echoed with `Access-Control-Allow-Credentials: true`.
- `F02.cors-disallowed`: a preflight from any other origin without API-key headers answers 400 `Disallowed CORS origin` with no `Access-Control-Allow-Origin`.
- `F02.cors-api-key`: a preflight asking for `x-session-api-key` or `authorization`, and any actual request carrying an API-key header (valid or not), gets `Access-Control-Allow-Origin: *` without credentials.
- `F02.cors-substring`: requested header names are matched whole: `x-my-authorization-token` stays on the strict policy.

## How to get to it (agent POV)

- REST: every route under `/api/automation/v1` except event ingestion
  (`POST /v1/events/{org_id}/{source}`, `GET /v1/events/{source}/requested-types`,
  signed instead, see the webhook and events families) and the KV routes
  (which also accept a per-run KV JWT, see the KV family). The probes in F01
  need nothing. The recipes below drive auth through `GET /v1`,
  `GET /v1/{automation_id}`, `DELETE /v1/uploads/{upload_id}` and the probes.
- Headers: `X-Session-API-Key: <key>` (what `api --auth key` sends, the
  default), `Authorization: Bearer <key>` (`--auth bearer`), cookies
  `openhands_auth` then legacy `keycloak_auth` (chunks `<name>_1`..`_7`
  are reassembled), `X-Org-Id` (SaaS path only).
- SDK and runs: a dispatched run receives the key as
  `OPENHANDS_AUTOMATION_API_KEY` and calls back with it; the OpenHands
  extensions' `automation` skill and Agent Canvas send `X-Session-API-Key`.
  Agent Canvas served from `localhost:3001` relies on the CORS rules.
- Config: `AUTOMATION_AGENT_SERVER_URL` (non-empty means local mode),
  `AUTOMATION_LOCAL_API_KEY`, `AUTOMATION_OPENHANDS_API_BASE_URL` (default
  `https://app.all-hands.dev`), `AUTOMATION_CORS_ORIGINS` (launch sets
  `http://localhost:3001,http://127.0.0.1:3001`), `AUTOMATION_AUTH_CACHE_TTL`
  (20 s), `AUTOMATION_AUTH_CACHE_SIZE`, `AUTOMATION_AUTH_MAX_RETRIES`.
- Translated: the upstream recipes stand a local HTTP sink in for the
  OpenHands app's `/api/v1/users/me` (`fixture http-sink`), which exercises
  the same code path a real SaaS deployment takes.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.
- `F02.cookie-upstream` restarts the service against a users/me stub; the
  bullets after it rely on that restart until `F02.upstream-errors`
  restores the default environment with `--reset-env`.

- **Missing credentials (`F02.missing-key`).** No header, then an empty session key, both on a protected route; the positive control gets in.
  ```sh
  control-automation api GET /v1 --expect 200 --check total ge 0 --quiet
  control-automation api GET /v1 --auth none --expect 401 --check detail eq 'Authentication required: provide Bearer token, X-Session-API-Key header, or session cookie' --check-header set-cookie missing --save F02.missing-key/no-header
  control-automation api GET /v1 --auth none --header 'X-Session-API-Key: ' --expect 401 --check detail matches '^Authentication required'
  ```
- **Session key (`F02.session-key`).** The right key in `X-Session-API-Key` gets in, a wrong one is refused.
  ```sh
  control-automation api GET /v1 --auth key --expect 200 --check automations exists --quiet
  control-automation api GET /v1 --auth bad --expect 401 --check detail eq 'Invalid API key' --save F02.session-key/wrong
  ```
- **Bearer key (`F02.bearer`).** Same key as Bearer; a wrong Bearer key is refused; other schemes are ignored and fall through to "no credential" unless a session key is also present.
  ```sh
  control-automation api GET /v1 --auth bearer --expect 200 --check automations exists --quiet --save F02.bearer/ok
  control-automation api GET /v1 --auth bearer-bad --expect 401 --check detail eq 'Invalid API key'
  control-automation exec --expect-output 'Authentication required' -- sh -c 'curl -s -H "Authorization: bearer $AUTOMATION_API_KEY" "$AUTOMATION_API_URL/v1"'
  control-automation exec --expect-output 'Authentication required' -- sh -c 'curl -s -H "Authorization: Token $AUTOMATION_API_KEY" "$AUTOMATION_API_URL/v1"'
  control-automation exec --expect-output '"automations"' -- sh -c 'curl -s -H "Authorization: Token qa-x" -H "X-Session-API-Key: $AUTOMATION_API_KEY" "$AUTOMATION_API_URL/v1"'
  ```
- **Bearer before session key (`F02.bearer-precedence`).** A wrong Bearer key masks a correct session key.
  ```sh
  control-automation exec --expect-output '"automations"' -- sh -c 'curl -s -H "X-Session-API-Key: $AUTOMATION_API_KEY" "$AUTOMATION_API_URL/v1"'
  control-automation exec --expect-output '{"detail":"Invalid API key"}' --save F02.bearer-precedence/masked -- sh -c 'curl -s -H "Authorization: Bearer qa-wrong" -H "X-Session-API-Key: $AUTOMATION_API_KEY" "$AUTOMATION_API_URL/v1"'
  ```
- **Empty Bearer key (`F02.empty-bearer`).** A non-breaking space survives HTTP trimming but not `str.strip()`, so it reaches the empty-key branch; a plain `Bearer ` does not.
  ```sh
  control-automation exec --expect-output '{"detail":"Empty API key"}' --save F02.empty-bearer/nbsp -- sh -c 'curl -s -H "$(printf "Authorization: Bearer \240")" "$AUTOMATION_API_URL/v1"'
  control-automation api GET /v1 --auth none --header 'Authorization: Bearer ' --expect 401 --check detail matches '^Authentication required'
  ```
- **Rotated key (`F02.rotated-key`).** Create an automation, rotate the key (both services restart), then the old key is refused, the new one gets in and the automation is still there.
  ```sh
  RAID=$(control-automation fixture automation --kind callback --name qa-auth-rotate --field id)
  control-automation restart --rotate-key
  control-automation api GET /v1 --auth previous --expect 401 --check detail eq 'Invalid API key' --save F02.rotated-key/previous
  control-automation api GET "/v1/$RAID" --auth key --expect 200 --check name eq qa-auth-rotate --save F02.rotated-key/current
  control-automation doctor
  ```
- **Fixed local identity (`F02.local-identity`).** Resources carry the uuid5 local user and org (`uuid5(NAMESPACE_DNS, "openhands-local-user")`, `...-org`); a garbage `X-Org-Id` changes nothing.
  ```sh
  LAID=$(control-automation fixture automation --kind callback --name qa-auth-owner --field id)
  control-automation api GET "/v1/$LAID" --expect 200 --check user_id eq c1a96d89-eae7-53bd-86ad-07f26266402b --check org_id eq 46d09c5c-7d7d-585f-8c37-a6b45a3de738 --save F02.local-identity/owner
  control-automation api GET "/v1/$LAID" --header 'X-Org-Id: not-a-uuid' --expect 200 --check id eq "$LAID"
  ```
- **Routes without a key (`F02.no-key-routes`).** Probes answer; event ingestion answers with its own signature rules, never the session-key 401.
  ```sh
  control-automation api GET /health --auth none --expect 200 --check status eq ok
  control-automation api GET /api/automation/ready --auth none --expect 200
  control-automation api POST /v1/events/46d09c5c-7d7d-585f-8c37-a6b45a3de738/qa-no-such-source --auth none --json '{"type":"qa.ping"}' --expect 404 --check detail eq 'Unknown webhook source: qa-no-such-source'
  control-automation api POST /v1/webhooks --json '{"name":"qa auth hook","source":"qa-auth-hook","webhook_secret":"qa-auth-hook-secret"}' --expect 201 --check source eq qa-auth-hook --quiet
  control-automation api POST /v1/events/46d09c5c-7d7d-585f-8c37-a6b45a3de738/qa-auth-hook --auth none --json '{"type":"qa.ping"}' --expect 401 --check detail eq 'Missing signature header: X-Signature-256' --save F02.no-key-routes/unsigned
  control-automation event send qa-auth-hook --secret qa-auth-hook-secret --json '{"type":"qa.ping"}' --expect 200 --save F02.no-key-routes/signed
  ```
- **Cookie goes upstream (`F02.cookie-upstream`).** Stand a users/me stub in for the OpenHands app (a user of another org with only `view_automations`), point the service at it, and send a cookie only.
  ```sh
  UM=$(control-automation fixture http-sink --name usersme --body '{"id":"11111111-1111-4111-8111-111111111111","org_id":"22222222-2222-4222-8222-222222222222","email":"qa-other@example.test","role":"member","permissions":["view_automations"]}' --field url)
  control-automation restart --service automation --env "AUTOMATION_OPENHANDS_API_BASE_URL=$UM"
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 200 --check total eq 0 --save F02.cookie-upstream/accepted
  control-automation sink read usersme --path /api/v1/users/me --contains 'openhands_auth=qa-cookie' --expect-min 1 --save F02.cookie-upstream/forwarded
  ```
  The stub saw `GET /api/v1/users/me` with the cookie forwarded under its own name.
- **Organization scoping (`F02.org-scoping`).** The stub's user (org `2222...`) cannot see the local org's automation; the local key still can.
  Requires: `F02.local-identity`, `F02.cookie-upstream`
  ```sh
  control-automation api GET "/v1/$LAID" --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 404 --check detail eq 'Automation not found' --save F02.org-scoping/other-org
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 200 --check automations len-eq 0
  control-automation api GET "/v1/$LAID" --expect 200 --check id eq "$LAID"
  ```
- **Permissions (`F02.permissions`).** Deleting an upload needs `manage_automations`, which the stub user lacks; the upload is still there afterwards. The upload is the one `F02.local-identity`'s fixture made.
  Requires: `F02.local-identity`, `F02.cookie-upstream`
  ```sh
  UPID=$(control-automation api GET /v1/uploads --expect 200 --field uploads.0.id)
  control-automation api DELETE "/v1/uploads/$UPID" --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 403 --check detail eq 'Requires manage_automations permission' --save F02.permissions/forbidden
  control-automation api GET "/v1/uploads/$UPID" --expect 200 --check status eq COMPLETED
  ```
- **X-Org-Id on the upstream path (`F02.org-header`).** A malformed org header is refused before upstream; a UUID is forwarded.
  Requires: `F02.cookie-upstream`
  ```sh
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --header 'X-Org-Id: nope' --expect 400 --check detail eq 'Invalid X-Org-Id header (must be a UUID)' --save F02.org-header/invalid
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --header 'X-Org-Id: 22222222-2222-4222-8222-222222222222' --expect 200
  control-automation sink read usersme --contains '"X-Org-Id": "22222222-2222-4222-8222-222222222222"' --expect-min 1
  ```
- **No upstream call for a wrong local key; cached upstream answers (`F02.auth-cache`).** One cookie request fills the cache, then a wrong local key and three more cookie requests (well inside the 20 s TTL) add no call to the stub.
  Requires: `F02.cookie-upstream`
  ```sh
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 200 --quiet
  N=$(wc -l < "$AUTOMATION_VERIFY_RUN/fixtures/usersme.jsonl")
  control-automation api GET /v1 --auth bad --expect 401 --check detail eq 'Invalid API key'
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 200 --quiet
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 200 --quiet
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 200 --quiet
  control-automation sink read usersme --expect-max "$N" --save F02.auth-cache/upstream-calls
  ```
- **Upstream refusals (`F02.upstream-errors`).** A stub answering 401, then a dead upstream port; finally restore the default environment.
  ```sh
  DENY=$(control-automation fixture http-sink --name usersme-deny --status 401 --body '{"detail":"no"}' --field url)
  control-automation restart --service automation --reset-env --env "AUTOMATION_OPENHANDS_API_BASE_URL=$DENY"
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 401 --check detail eq 'Invalid or expired session cookie' --save F02.upstream-errors/upstream-401
  control-automation restart --service automation --reset-env --env AUTOMATION_OPENHANDS_API_BASE_URL=http://127.0.0.1:9
  control-automation api GET /v1 --auth none --header 'Cookie: openhands_auth=qa-cookie' --expect 502 --check detail eq 'Failed to reach OpenHands API for authentication' --save F02.upstream-errors/unreachable
  control-automation restart --service automation --reset-env
  control-automation api GET /v1 --expect 200 --quiet
  ```
- **CORS allowlist (`F02.cors-allowlist`).** A preflight from an allowlisted origin, and an actual unauthenticated request from the other one.
  ```sh
  control-automation api OPTIONS /v1 --auth none --header 'Origin: http://localhost:3001' --header 'Access-Control-Request-Method: GET' --expect 200 --check-header access-control-allow-origin eq http://localhost:3001 --check-header access-control-allow-credentials eq true --save F02.cors-allowlist/preflight
  control-automation api GET /health --auth none --header 'Origin: http://127.0.0.1:3001' --expect 200 --check-header access-control-allow-origin eq http://127.0.0.1:3001 --check-header access-control-allow-credentials eq true
  ```
- **CORS disallowed origin (`F02.cors-disallowed`).** The same preflight from an unlisted origin is refused, and an actual request from it gets no allow-origin header.
  ```sh
  control-automation api OPTIONS /v1 --auth none --header 'Origin: http://qa-evil.example' --header 'Access-Control-Request-Method: GET' --expect 400 --check-header access-control-allow-origin missing --save F02.cors-disallowed/preflight
  control-automation api GET /health --auth none --header 'Origin: http://qa-evil.example' --expect 200 --check-header access-control-allow-origin missing
  ```
  The 400 body is the plain text `Disallowed CORS origin`.
- **CORS for API-key callers (`F02.cors-api-key`).** Preflights that ask for a key header, from any origin, get the wildcard without credentials; so do actual requests that carry a key, even a wrong one.
  ```sh
  control-automation api OPTIONS /v1 --auth none --header 'Origin: http://qa-evil.example' --header 'Access-Control-Request-Method: GET' --header 'Access-Control-Request-Headers: x-session-api-key' --expect 200 --check-header access-control-allow-origin eq '*' --check-header access-control-allow-credentials missing --save F02.cors-api-key/preflight
  control-automation api OPTIONS /v1 --auth none --header 'Origin: http://qa-evil.example' --header 'Access-Control-Request-Method: POST' --header 'Access-Control-Request-Headers: content-type, Authorization' --expect 200 --check-header access-control-allow-origin eq '*'
  control-automation api GET /v1 --header 'Origin: http://qa-evil.example' --expect 200 --check-header access-control-allow-origin eq '*' --check-header access-control-allow-credentials missing --quiet
  control-automation api GET /v1 --auth bad --header 'Origin: http://qa-evil.example' --expect 401 --check-header access-control-allow-origin eq '*'
  ```
- **Whole header names only (`F02.cors-substring`).** A requested header that merely contains `authorization` keeps the strict policy.
  ```sh
  control-automation api OPTIONS /v1 --auth none --header 'Origin: http://qa-evil.example' --header 'Access-Control-Request-Method: GET' --header 'Access-Control-Request-Headers: x-my-authorization-token' --expect 400 --check-header access-control-allow-origin missing --save F02.cors-substring/strict
  ```

## Gotchas

- `AUTOMATION_LOCAL_API_KEY` empty in local mode means every key is checked
  against the OpenHands app (`AUTOMATION_OPENHANDS_API_BASE_URL`), not
  refused; the run always sets it.
- In cloud mode (`AUTOMATION_AGENT_SERVER_URL` empty) the local key is not
  special: it is forwarded upstream as `Authorization: Bearer` like any other
  key (seen live against the stub, which accepted it as the stub's user).
- HTTP trims trailing spaces from header values, so `Authorization: Bearer `
  never produces `Empty API key`; only a value whose remainder is whitespace
  to Python but not to HTTP (for example `\xa0`) does.
- The Bearer prefix is case-sensitive for auth (`Bearer `) but
  case-insensitive for CORS (`bearer `): a lowercase `bearer <key>` request
  gets the CORS wildcard and then a 401.
- Any API-key header, valid or not, selects the permissive CORS policy; the
  wildcard is safe only because credentials are off on that policy.
- The upstream auth cache (20 s per method + org + credential) means a
  revoked SaaS key keeps working until the entry expires. Only successes are
  cached.
- 403 paths (`Requires ... permission`) cannot happen with the local key: the
  local user has both permissions. They need the upstream path.
- `restart --rotate-key` restarts both services; `--reset-env` also drops
  any `--env` from launch. A cookie stub left configured makes later
  cookie-less tests behave normally, but always restore with `--reset-env`.

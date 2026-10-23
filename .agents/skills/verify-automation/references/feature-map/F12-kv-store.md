# Per-automation KV store

A small, encrypted key-value store that belongs to one automation. A running
automation script reads and writes it with the JWT the dispatcher injects as
`AUTOMATION_KV_TOKEN` (no user key needed); a user, Agent Canvas or a spawned
conversation reaches the same data with the normal API key plus
`?automation_id=`. Values are arbitrary strict JSON (objects, lists, numbers,
strings, booleans) encrypted at rest with `AUTOMATION_KV_SECRET`; the store
offers Redis-like operations (conditional set, nested-path get and patch,
counters, lists, atomic batches) and one version counter per automation for
optimistic concurrency. Without `AUTOMATION_KV_SECRET` every route answers 503.

Source: `openhands/automation/kv_router.py`, `openhands/automation/kv_helpers.py`, `openhands/automation/kv_schemas.py`, `openhands/automation/kv_metrics.py`, `openhands/automation/utils/kv.py`, `docs/kv-store-design.md`, `docs/kv-store-client-guide.md`

Routes: `GET /api/automation/v1/kv`, `GET /api/automation/v1/kv/{key}`,
`PUT /api/automation/v1/kv/{key}`, `PATCH /api/automation/v1/kv/{key}`,
`DELETE /api/automation/v1/kv/{key}`, `POST /api/automation/v1/kv/{key}/incr`,
`POST /api/automation/v1/kv/{key}/decr`, `POST /api/automation/v1/kv/{key}/lpush`,
`POST /api/automation/v1/kv/{key}/rpush`, `POST /api/automation/v1/kv/{key}/lpop`,
`POST /api/automation/v1/kv/{key}/rpop`, `GET /api/automation/v1/kv/{key}/len`,
`POST /api/automation/v1/kv/batch`

## Sub-features

- `F12.user-auth`: the local API key (X-Session-API-Key or Bearer) plus `?automation_id=` of an own automation reads and writes the store; `GET /v1/capabilities` lists `kvStore`.
- `F12.auth-errors`: no credential or a wrong key gives 401 `Authentication required: provide a KV token or user credentials`; a missing `automation_id` gives 400; an unknown automation gives 404; refused writes store nothing.
- `F12.jwt-auth`: the run's HS256 JWT (`Authorization: Bearer <jwt>`) reaches its automation's store with no query parameter and sees the same data as user auth; `exp` is optional.
- `F12.jwt-invalid`: a JWT signed with another secret, an expired one, one without `automation_id`, or any Bearer value with exactly two dots gives 401 `Invalid or expired KV token`.
- `F12.not-configured`: with `AUTOMATION_KV_SECRET` empty every KV route answers 503 (user key and JWT alike) and `kvStore` leaves the capabilities; stored data is back once the secret is restored.
- `F12.put-get`: a first PUT answers 201 `created:true`, an overwrite 200 `created:false`; GET returns the exact JSON for every type; a missing key is 404 `key_not_found`.
- `F12.put-null`: a JSON `null` PUT body is refused with 422 `Field required` (the body counts as missing); a batch `set` stores `null` fine.
- `F12.get-path`: `GET ?path=` reads a nested value by dot path, list index or bracket notation; a missing path is 404 `invalid_path`.
- `F12.get-meta`: `GET ?meta=true` adds the automation-wide `version` and ISO `created_at`/`updated_at`.
- `F12.list`: `GET /v1/kv` lists keys sorted with `count` and `total`; `limit`/`offset` page; `limit` outside 1..1000 is 422.
- `F12.key-validation`: keys starting with `$`, over 255 characters, whitespace-only or with control characters answer 400 `invalid_key: ...`; a key with `/` never reaches the store (404).
- `F12.value-validation`: values nested deeper than 32 levels and NaN answer 400 `invalid_value: ...` and store nothing.
- `F12.put-nx-xx`: `nx=true` on an existing key is 409 `{key, created:false, error:"key_exists"}` (no `detail`); `xx=true` on a missing key is 409 `key_not_exists`; both together are 400.
- `F12.if-version`: a stale `if_version` on PUT, PATCH, DELETE or batch is 409 `version_mismatch` with `Retry-After: 1` and changes nothing; the matching version succeeds and adds 1.
- `F12.version-global`: every single-key write bumps one version shared by all keys of the automation; a batch bumps it once; a pop of a missing key and a delete of a missing key do not bump it.
- `F12.size-limit`: a value larger than `AUTOMATION_KV_MAX_VALUE_SIZE` bytes (compact JSON) is 413 `value_too_large` on PUT, list pushes and batches; nothing is stored.
- `F12.patch`: PATCH `{path, value}` sets a nested path inside an object value, creating intermediate objects; missing key 404, non-object 400, non-object intermediate 400, unclosed bracket 400.
- `F12.counters`: incr/decr start a missing key at `±by`, accumulate, and refuse floats, booleans and strings with 400 `type_mismatch`.
- `F12.lists`: lpush/rpush/lpop/rpop/len behave like Redis lists; popping a missing key returns `value:null`; list ops on a non-list are 400; `len` of a missing key is 404.
- `F12.delete`: DELETE of an existing key answers `deleted:true` and the key is gone; a missing key answers 200 `deleted:false`.
- `F12.batch`: `POST /v1/kv/batch` runs set/incr/rpush/patch/delete/pop in order and answers `version` plus one result per op with op-specific fields.
- `F12.batch-atomic`: when one op fails, the batch answers 400 `operation_failed` with `operation_index`, and none of the earlier ops is persisted.
- `F12.batch-validation`: empty or >100 ops, a key over 255 characters and `nx`+`xx` are 422; a `$` key, `set nx` on an existing key and `set xx` on a missing key are 400 `operation_failed`; a batch `patch` creates a missing key (single PATCH does not).
- `F12.isolation`: keys of one automation are invisible to another, by user key and by JWT; a JWT ignores `?automation_id=` and always acts on its own claim.
- `F12.encrypted-at-rest`: `automation_kv.value_encrypted` holds a Fernet token (`gAAAAA...`), never the plaintext.
- `F12.soft-deleted-automation`: after `DELETE /v1/{id}` (a soft delete) the automation is 404 but its KV data is still readable with `?automation_id=`.
- `F12.run-uses-kv`: a dispatched run writes the store with its injected `AUTOMATION_KV_TOKEN`; two runs leave `qa-runs` at 2 and `qa-last-run` naming the second run.
- `F12.get-path-malformed`: `GET ?path=a[b` (unclosed bracket) should answer 400 like PATCH does; it answers 500 (known bug).
- `F12.patch-empty-path`: PATCH with `path:""` should answer 400 `invalid_path`; it answers 500 (known bug).
- `F12.jwt-unknown-automation`: a validly signed JWT for an automation that does not exist reads as empty, but its first write should be refused with a 4xx; it answers 500 (known bug).

## How to get to it (agent POV)

- REST: the routes above, under `/api/automation/v1/kv`. Every recipe below
  drives them with the `api` verb; user-auth recipes pass
  `--query automation_id=$AID`, JWT recipes pass
  `--auth none --header "Authorization: Bearer $TOKEN"`.
- Inside a run: the dispatcher injects `AUTOMATION_KV_TOKEN` (HS256, claim
  `automation_id`, 24 h expiry; `dispatcher.py`, `utils/kv.py:create_kv_token`)
  and `AUTOMATION_API_URL`; a script calls `$AUTOMATION_API_URL/v1/kv/<key>`
  with `Authorization: Bearer $AUTOMATION_KV_TOKEN`. `fixture automation --kind kv`
  does exactly that (`F12.run-uses-kv`). `kv token` mints the same token
  for direct driving (`--wrong-secret`, `--expired`, `--no-exp`, no
  `--automation-id` for a missing claim).
- Users, Agent Canvas and spawned conversations: the normal API key
  (`X-Session-API-Key` or `Authorization: Bearer <key>`) plus
  `?automation_id=`; membership in the automation's org is the check.
- Capability discovery: `GET /v1/capabilities` lists `kvStore` in
  `features` only while the secret is set.
- Config: `AUTOMATION_KV_SECRET` (required; signs tokens, encrypts values),
  `AUTOMATION_KV_MAX_VALUE_SIZE` (default 65536 bytes per value, 0 disables),
  `AUTOMATION_KV_LOCK_TIMEOUT_MS` (PostgreSQL only). `launch` writes a
  random secret into the run, so the store is on by default.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.
- Two fixture automations, A and B, and a KV JWT for A, created first:
  ```sh
  AID=$(control-automation fixture automation --kind kv --name qa-kv-a --field id)
  BID=$(control-automation fixture automation --kind kv --name qa-kv-b --field id)
  TOKEN=$(control-automation kv token --automation-id "$AID")
  ```
- Every bullet uses its own keys (`qa-<bullet>-...`) so bullets do not see
  each other's data. The size-limit and not-configured bullets restart the
  service and restore it with `restart --service automation --reset-env`.

- **User auth (`F12.user-auth`).** The run's key reaches A's store both as X-Session-API-Key and as Bearer, and the store is advertised.
  ```sh
  control-automation api GET /v1/capabilities --expect 200 --quiet --check features contains kvStore
  control-automation api PUT /v1/kv/qa-ua --query automation_id="$AID" --json '{"hello":"world"}' --expect 201 --check created eq true --save F12.user-auth/put
  control-automation api GET /v1/kv/qa-ua --query automation_id="$AID" --expect 200 --check value.hello eq world --save F12.user-auth/get
  control-automation api GET /v1/kv/qa-ua --auth bearer --query automation_id="$AID" --expect 200 --check value.hello eq world
  ```
  PUT answers 201, and GET returns the same object with either header.
- **Auth errors (`F12.auth-errors`).** Each refusal has its own status and message, and a refused write stores nothing.
  ```sh
  control-automation api GET /v1/kv --auth none --query automation_id="$AID" --expect 401 --check detail eq 'Authentication required: provide a KV token or user credentials' --save F12.auth-errors/no-credential
  control-automation api GET /v1/kv --auth bad --query automation_id="$AID" --expect 401 --check detail eq 'Authentication required: provide a KV token or user credentials'
  control-automation api GET /v1/kv --auth bearer-bad --query automation_id="$AID" --expect 401 --check detail eq 'Authentication required: provide a KV token or user credentials'
  control-automation api GET /v1 --auth bad --expect 401 --check detail eq 'Invalid API key'
  control-automation api GET /v1/kv --expect 400 --check detail eq 'automation_id query parameter is required for user-authenticated KV access' --save F12.auth-errors/no-automation-id
  control-automation api GET /v1/kv --query automation_id=00000000-0000-4000-8000-000000000000 --expect 404 --check detail eq 'Automation not found' --save F12.auth-errors/unknown-automation
  control-automation api PUT /v1/kv/qa-ae --auth none --query automation_id="$AID" --json '"x"' --expect 401
  control-automation api GET /v1/kv/qa-ae --query automation_id="$AID" --expect 404 --check detail eq key_not_found
  ```
  A wrong key gets the KV message, not the `Invalid API key` that `/v1`
  answers for the same key. The refused PUT leaves no key behind.
- **JWT auth (`F12.jwt-auth`).** A's token writes without a query parameter; user auth reads what it wrote, and the token reads what the user wrote.
  ```sh
  control-automation api PUT /v1/kv/qa-jwt --auth none --header "Authorization: Bearer $TOKEN" --json '{"via":"jwt"}' --expect 201 --save F12.jwt-auth/put
  control-automation api GET /v1/kv/qa-jwt --query automation_id="$AID" --expect 200 --check value.via eq jwt
  control-automation api PUT /v1/kv/qa-jwt-user --query automation_id="$AID" --json '"via-user"' --expect 201
  control-automation api GET /v1/kv/qa-jwt-user --auth none --header "Authorization: Bearer $TOKEN" --expect 200 --check value eq via-user --save F12.jwt-auth/get
  NOEXP=$(control-automation kv token --automation-id "$AID" --no-exp)
  control-automation api GET /v1/kv/qa-jwt --auth none --header "Authorization: Bearer $NOEXP" --expect 200 --check value.via eq jwt
  ```
- **Bad JWTs (`F12.jwt-invalid`).** The good token gets in; every bad one is refused with the same 401.
  ```sh
  control-automation api GET /v1/kv --auth none --header "Authorization: Bearer $TOKEN" --expect 200 --check keys exists
  WRONG=$(control-automation kv token --automation-id "$AID" --wrong-secret)
  control-automation api GET /v1/kv --auth none --header "Authorization: Bearer $WRONG" --expect 401 --check detail eq 'Invalid or expired KV token' --save F12.jwt-invalid/wrong-secret
  EXPIRED=$(control-automation kv token --automation-id "$AID" --expired)
  control-automation api GET /v1/kv --auth none --header "Authorization: Bearer $EXPIRED" --expect 401 --check detail eq 'Invalid or expired KV token' --save F12.jwt-invalid/expired
  NOCLAIM=$(control-automation kv token)
  control-automation api GET /v1/kv --auth none --header "Authorization: Bearer $NOCLAIM" --expect 401 --check detail eq 'Invalid or expired KV token'
  control-automation api GET /v1/kv --auth none --header 'Authorization: Bearer not.a.jwt' --query automation_id="$AID" --expect 401 --check detail eq 'Invalid or expired KV token'
  ```
  `not.a.jwt` has two dots, so it is judged as a JWT and never falls back to
  user auth, even with `automation_id` set.
- **Not configured (`F12.not-configured`).** Empty the secret, probe with both credentials, then restore it.
  ```sh
  control-automation api PUT /v1/kv/qa-nc --query automation_id="$AID" --json '"before"' --expect 201
  control-automation restart --service automation --env AUTOMATION_KV_SECRET=
  control-automation api GET /v1/kv --query automation_id="$AID" --expect 503 --check detail eq 'KV store not configured (missing AUTOMATION_KV_SECRET)' --save F12.not-configured/user
  control-automation api GET /v1/kv/qa-nc --auth none --header "Authorization: Bearer $TOKEN" --expect 503 --check detail eq 'KV store not configured (missing AUTOMATION_KV_SECRET)'
  control-automation api PUT /v1/kv/qa-nc --query automation_id="$AID" --json '"during"' --expect 503
  control-automation api GET /v1/capabilities --expect 200 --quiet --check features not-contains kvStore
  control-automation restart --service automation --reset-env
  control-automation api GET /v1/kv/qa-nc --query automation_id="$AID" --expect 200 --check value eq before
  control-automation api GET /v1/capabilities --expect 200 --quiet --check features contains kvStore
  ```
  An empty value really disables the store (503 even with a valid key or
  token); after restoring, the value written before is unchanged.
- **PUT and GET (`F12.put-get`).** Create, overwrite, read back each JSON type, and miss.
  ```sh
  control-automation api PUT /v1/kv/qa-pg --query automation_id="$AID" --json '{"n":1,"l":[1,"two",true],"s":"x"}' --expect 201 --check created eq true --check value.n eq 1 --save F12.put-get/create
  control-automation api PUT /v1/kv/qa-pg --query automation_id="$AID" --json '{"n":2}' --expect 200 --check created eq false --check updated_at exists --save F12.put-get/overwrite
  control-automation api GET /v1/kv/qa-pg --query automation_id="$AID" --expect 200 --check value.n eq 2 --check value.l missing
  control-automation api PUT /v1/kv/qa-pg-list --query automation_id="$AID" --json '[1,"a",false]' --expect 201
  control-automation api GET /v1/kv/qa-pg-list --query automation_id="$AID" --expect 200 --check value len-eq 3 --check value.1 eq a
  control-automation api PUT /v1/kv/qa-pg-str --query automation_id="$AID" --json '"plain"' --expect 201
  control-automation api GET /v1/kv/qa-pg-str --query automation_id="$AID" --expect 200 --check value eq plain
  control-automation api PUT /v1/kv/qa-pg-num --query automation_id="$AID" --json '42' --expect 201
  control-automation api GET /v1/kv/qa-pg-num --query automation_id="$AID" --expect 200 --check value eq 42
  control-automation api GET /v1/kv/qa-pg-missing --query automation_id="$AID" --expect 404 --check detail eq key_not_found
  ```
  An overwrite replaces the whole value (the old `l` is gone).
- **JSON null (`F12.put-null`).** PUT cannot store `null`; a batch can.
  ```sh
  control-automation api PUT /v1/kv/qa-null --query automation_id="$AID" --data 'null' --content-type application/json --expect 422 --check detail.0.type eq missing --save F12.put-null/put
  control-automation api GET /v1/kv/qa-null --query automation_id="$AID" --expect 404
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"set","key":"qa-null","value":null}]}' --expect 200 --check results.0.created eq true
  control-automation api GET /v1/kv/qa-null --query automation_id="$AID" --expect 200 --check value eq null --save F12.put-null/batch-read
  ```
- **Nested path reads (`F12.get-path`).** Dot path, list index and bracket notation for a key containing a dot.
  ```sh
  control-automation api PUT /v1/kv/qa-path --query automation_id="$AID" --json '{"items":[{"n":"x"}],"cfg":{"x.y":7},"a":{"b":{"c":3}}}' --expect 201
  control-automation api GET /v1/kv/qa-path --query automation_id="$AID" --query path=a.b.c --expect 200 --check path eq a.b.c --check value eq 3 --save F12.get-path/dot
  control-automation api GET /v1/kv/qa-path --query automation_id="$AID" --query path=items.0.n --expect 200 --check value eq x
  control-automation api GET /v1/kv/qa-path --query automation_id="$AID" --query 'path=cfg["x.y"]' --expect 200 --check value eq 7
  control-automation api GET /v1/kv/qa-path --query automation_id="$AID" --query path=a.zz --expect 404 --check detail eq invalid_path --save F12.get-path/missing
  ```
- **Metadata (`F12.get-meta`).** `meta=true` adds the version and timestamps.
  ```sh
  control-automation api PUT /v1/kv/qa-meta --query automation_id="$AID" --json '"m"' --expect 201
  control-automation api GET /v1/kv/qa-meta --query automation_id="$AID" --query meta=true --expect 200 --check value eq m --check version ge 1 --check created_at matches '^20[0-9]{2}-' --check updated_at matches '^20[0-9]{2}-' --save F12.get-meta/meta
  control-automation api GET /v1/kv/qa-meta --query automation_id="$AID" --expect 200 --check version missing
  ```
- **Listing and paging (`F12.list`).** B starts empty here, so its listing is exact.
  ```sh
  control-automation api PUT /v1/kv/qa-l-c --query automation_id="$BID" --json '3' --expect 201
  control-automation api PUT /v1/kv/qa-l-a --query automation_id="$BID" --json '1' --expect 201
  control-automation api PUT /v1/kv/qa-l-b --query automation_id="$BID" --json '2' --expect 201
  control-automation api GET /v1/kv --query automation_id="$BID" --expect 200 --check total eq 3 --check count eq 3 --check keys.0 eq qa-l-a --check keys.2 eq qa-l-c --check limit eq 100 --save F12.list/all
  control-automation api GET /v1/kv --query automation_id="$BID" --query limit=1 --query offset=1 --expect 200 --check keys len-eq 1 --check keys.0 eq qa-l-b --check count eq 1 --check total eq 3 --save F12.list/page
  control-automation api GET /v1/kv --query automation_id="$BID" --query limit=0 --expect 422
  control-automation api GET /v1/kv --query automation_id="$BID" --query limit=1001 --expect 422
  ```
- **Key validation (`F12.key-validation`).** Each bad key has its own 400 message.
  ```sh
  control-automation api PUT '/v1/kv/$qa' --query automation_id="$AID" --json '1' --expect 400 --check detail eq "invalid_key: keys starting with '\$' are reserved for system use" --save F12.key-validation/dollar
  LONGKEY=$(printf 'k%.0s' $(seq 256))
  control-automation api PUT "/v1/kv/$LONGKEY" --query automation_id="$AID" --json '1' --expect 400 --check detail eq 'invalid_key: key exceeds 255 chars (256 given)'
  control-automation api GET '/v1/kv/%20%20' --query automation_id="$AID" --expect 400 --check detail eq 'invalid_key: key cannot be whitespace-only'
  control-automation api GET '/v1/kv/qa%01x' --query automation_id="$AID" --expect 400 --check detail contains 'control character' --check detail contains 'at position 2'
  control-automation exec --expect-output 404 -- sh -c 'curl -s -o /dev/null -w "%{http_code}" -H "X-Session-API-Key: $AUTOMATION_API_KEY" "$AUTOMATION_URL/api/automation/v1/kv/qa/slash?automation_id='"$AID"'"'
  ```
- **Value validation (`F12.value-validation`).** Too-deep and NaN values are refused and leave nothing behind; exactly 32 levels is fine.
  ```sh
  DEEP=$(printf '[%.0s' $(seq 33))1$(printf ']%.0s' $(seq 33))
  control-automation api PUT /v1/kv/qa-deep --query automation_id="$AID" --json "$DEEP" --expect 400 --check detail eq 'invalid_value: Value exceeds maximum nesting depth of 32' --save F12.value-validation/deep
  OK32=$(printf '[%.0s' $(seq 32))1$(printf ']%.0s' $(seq 32))
  control-automation api PUT /v1/kv/qa-deep-ok --query automation_id="$AID" --json "$OK32" --expect 201
  control-automation api PUT /v1/kv/qa-nan --query automation_id="$AID" --data '{"x":NaN}' --content-type application/json --expect 400 --check detail contains 'invalid_value: Value contains non-JSON-compliant data' --save F12.value-validation/nan
  control-automation api GET /v1/kv/qa-deep --query automation_id="$AID" --expect 404
  control-automation api GET /v1/kv/qa-nan --query automation_id="$AID" --expect 404
  ```
- **Conditional set (`F12.put-nx-xx`).** `nx` and `xx` gate creation and update; their 409 bodies differ in shape.
  ```sh
  control-automation api PUT /v1/kv/qa-nx --query automation_id="$AID" --query nx=true --json '"first"' --expect 201 --check created eq true
  control-automation api PUT /v1/kv/qa-nx --query automation_id="$AID" --query nx=true --json '"second"' --expect 409 --check error eq key_exists --check created eq false --check detail missing --save F12.put-nx-xx/nx-existing
  control-automation api GET /v1/kv/qa-nx --query automation_id="$AID" --expect 200 --check value eq first
  control-automation api PUT /v1/kv/qa-xx --query automation_id="$AID" --query xx=true --json '"x"' --expect 409 --check detail eq key_not_exists --save F12.put-nx-xx/xx-missing
  control-automation api GET /v1/kv/qa-xx --query automation_id="$AID" --expect 404
  control-automation api PUT /v1/kv/qa-nx --query automation_id="$AID" --query xx=true --json '"updated"' --expect 200 --check created eq false
  control-automation api GET /v1/kv/qa-nx --query automation_id="$AID" --expect 200 --check value eq updated
  control-automation api PUT /v1/kv/qa-nx --query automation_id="$AID" --query nx=true --query xx=true --json '"x"' --expect 400 --check detail eq 'Cannot use both nx and xx'
  ```
- **Optimistic concurrency (`F12.if-version`).** A stale version is refused on every write path with `Retry-After: 1`; the current one succeeds.
  ```sh
  control-automation api PUT /v1/kv/qa-cas --query automation_id="$AID" --json '{"v":1}' --expect 201
  V=$(control-automation api GET /v1/kv/qa-cas --query automation_id="$AID" --query meta=true --expect 200 --field version)
  STALE=$((V - 1))
  control-automation api PUT /v1/kv/qa-cas --query automation_id="$AID" --query if_version="$STALE" --json '{"v":2}' --expect 409 --check detail.error eq version_mismatch --check detail.expected_version eq "$STALE" --check detail.actual_version eq "$V" --check-header retry-after eq 1 --save F12.if-version/put-stale
  control-automation api PATCH /v1/kv/qa-cas --query automation_id="$AID" --query if_version="$STALE" --json '{"path":"v","value":3}' --expect 409 --check detail.error eq version_mismatch --check-header retry-after eq 1
  control-automation api DELETE /v1/kv/qa-cas --query automation_id="$AID" --query if_version="$STALE" --expect 409 --check detail.error eq version_mismatch --check-header retry-after eq 1
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json "{\"if_version\":$STALE,\"operations\":[{\"op\":\"set\",\"key\":\"qa-cas\",\"value\":9}]}" --expect 409 --check detail.error eq version_mismatch --check-header retry-after eq 1
  control-automation api GET /v1/kv/qa-cas --query automation_id="$AID" --query meta=true --expect 200 --check value.v eq 1 --check version eq "$V"
  control-automation api PUT /v1/kv/qa-cas --query automation_id="$AID" --query if_version="$V" --json '{"v":2}' --expect 200 --save F12.if-version/put-current
  control-automation api GET /v1/kv/qa-cas --query automation_id="$AID" --query meta=true --expect 200 --check value.v eq 2 --check version eq $((V + 1))
  ```
  None of the four refused writes changed the value or the version.
- **One version per automation (`F12.version-global`).** Writes to another key move the version; a batch moves it once; no-op pops and deletes do not.
  ```sh
  control-automation api PUT /v1/kv/qa-ver-a --query automation_id="$AID" --json '1' --expect 201
  V0=$(control-automation api GET /v1/kv/qa-ver-a --query automation_id="$AID" --query meta=true --expect 200 --field version)
  control-automation api PUT /v1/kv/qa-ver-b --query automation_id="$AID" --json '1' --expect 201
  control-automation api GET /v1/kv/qa-ver-a --query automation_id="$AID" --query meta=true --expect 200 --check version eq $((V0 + 1)) --save F12.version-global/other-key
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"incr","key":"qa-ver-a"},{"op":"incr","key":"qa-ver-b"},{"op":"set","key":"qa-ver-c","value":0}]}' --expect 200 --check version eq $((V0 + 2)) --check results len-eq 3
  control-automation api GET /v1/kv/qa-ver-b --query automation_id="$AID" --query meta=true --expect 200 --check version eq $((V0 + 2)) --check value eq 2
  control-automation api POST /v1/kv/qa-ver-none/lpop --query automation_id="$AID" --expect 200 --check value eq null
  control-automation api DELETE /v1/kv/qa-ver-none --query automation_id="$AID" --expect 200 --check deleted eq false
  control-automation api GET /v1/kv/qa-ver-a --query automation_id="$AID" --query meta=true --expect 200 --check version eq $((V0 + 2)) --save F12.version-global/no-op
  ```
- **Per-value size limit (`F12.size-limit`).** Shrink the limit to 64 bytes, probe PUT, rpush and batch, then restore it.
  ```sh
  control-automation restart --service automation --env AUTOMATION_KV_MAX_VALUE_SIZE=64
  BIG='"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'
  control-automation api PUT /v1/kv/qa-size --query automation_id="$AID" --json "$BIG" --expect 413 --check detail.error eq value_too_large --check detail.size eq 82 --check detail.limit eq 64 --check detail.key eq qa-size --save F12.size-limit/put
  control-automation api GET /v1/kv/qa-size --query automation_id="$AID" --expect 404
  control-automation api PUT /v1/kv/qa-size --query automation_id="$AID" --json '"small"' --expect 201
  control-automation api POST /v1/kv/qa-size-list/rpush --query automation_id="$AID" --json "{\"value\":$BIG}" --expect 413 --check detail.error eq value_too_large
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json "{\"operations\":[{\"op\":\"set\",\"key\":\"qa-size-b\",\"value\":$BIG}]}" --expect 413 --check detail.key eq qa-size-b
  control-automation api GET /v1/kv/qa-size-b --query automation_id="$AID" --expect 404
  control-automation restart --service automation --reset-env
  control-automation api PUT /v1/kv/qa-size --query automation_id="$AID" --json "$BIG" --expect 200 --check created eq false
  ```
  The 80-character string is 82 bytes as JSON (the quotes count). Under the
  default limit (65536) the same value is accepted.
- **PATCH (`F12.patch`).** Set a nested path in an object, then each refusal.
  ```sh
  control-automation api PUT /v1/kv/qa-patch --query automation_id="$AID" --json '{"a":{"b":1}}' --expect 201
  control-automation api PATCH /v1/kv/qa-patch --query automation_id="$AID" --json '{"path":"db.port","value":5432}' --expect 200 --check path eq db.port --check value eq 5432 --save F12.patch/patch
  control-automation api GET /v1/kv/qa-patch --query automation_id="$AID" --expect 200 --check value.db.port eq 5432 --check value.a.b eq 1 --save F12.patch/read-back
  control-automation api PATCH /v1/kv/qa-patch-missing --query automation_id="$AID" --json '{"path":"a","value":1}' --expect 404 --check detail eq key_not_found
  control-automation api PUT /v1/kv/qa-patch-num --query automation_id="$AID" --json '5' --expect 201
  control-automation api PATCH /v1/kv/qa-patch-num --query automation_id="$AID" --json '{"path":"a","value":1}' --expect 400 --check detail eq 'type_mismatch: value is not an object'
  control-automation api PATCH /v1/kv/qa-patch --query automation_id="$AID" --json '{"path":"a.b.c","value":1}' --expect 400 --check detail eq "invalid_path: Cannot set path 'a.b.c': intermediate value is not a dict"
  control-automation api PATCH /v1/kv/qa-patch --query automation_id="$AID" --json '{"path":"a[b","value":1}' --expect 400 --check detail eq "invalid_path: Invalid path: unclosed bracket in 'a[b'"
  control-automation api GET /v1/kv/qa-patch --query automation_id="$AID" --expect 200 --check value.a.b eq 1
  ```
- **Counters (`F12.counters`).** incr/decr from nothing, with `by`, accumulating; non-integers refused.
  ```sh
  control-automation api POST /v1/kv/qa-cnt/incr --query automation_id="$AID" --expect 200 --check value eq 1 --save F12.counters/incr-new
  control-automation api POST /v1/kv/qa-cnt/incr --query automation_id="$AID" --json '{"by":5}' --expect 200 --check value eq 6
  control-automation api POST /v1/kv/qa-cnt/decr --query automation_id="$AID" --json '{"by":2}' --expect 200 --check value eq 4
  control-automation api GET /v1/kv/qa-cnt --query automation_id="$AID" --expect 200 --check value eq 4 --save F12.counters/read-back
  control-automation api POST /v1/kv/qa-cnt-neg/decr --query automation_id="$AID" --expect 200 --check value eq -1
  control-automation api PUT /v1/kv/qa-cnt-f --query automation_id="$AID" --json '1.5' --expect 201
  control-automation api POST /v1/kv/qa-cnt-f/incr --query automation_id="$AID" --expect 400 --check detail eq 'type_mismatch: value is float, not integer (integer required)'
  control-automation api PUT /v1/kv/qa-cnt-b --query automation_id="$AID" --json 'true' --expect 201
  control-automation api POST /v1/kv/qa-cnt-b/decr --query automation_id="$AID" --expect 400 --check detail eq 'type_mismatch: value is boolean, not integer'
  control-automation api PUT /v1/kv/qa-cnt-s --query automation_id="$AID" --json '"7"' --expect 201
  control-automation api POST /v1/kv/qa-cnt-s/incr --query automation_id="$AID" --expect 400 --check detail eq 'type_mismatch: value is not an integer'
  control-automation api GET /v1/kv/qa-cnt-f --query automation_id="$AID" --expect 200 --check value eq 1.5
  ```
- **Lists (`F12.lists`).** Push both ends, pop both ends, length, and the refusals.
  ```sh
  control-automation api POST /v1/kv/qa-list/rpush --query automation_id="$AID" --json '{"value":"a"}' --expect 200 --check length eq 1
  control-automation api POST /v1/kv/qa-list/rpush --query automation_id="$AID" --json '{"value":"b"}' --expect 200 --check length eq 2
  control-automation api POST /v1/kv/qa-list/lpush --query automation_id="$AID" --json '{"value":"z"}' --expect 200 --check length eq 3 --save F12.lists/lpush
  control-automation api GET /v1/kv/qa-list --query automation_id="$AID" --expect 200 --check value.0 eq z --check value.2 eq b
  control-automation api POST /v1/kv/qa-list/lpop --query automation_id="$AID" --expect 200 --check value eq z
  control-automation api POST /v1/kv/qa-list/rpop --query automation_id="$AID" --expect 200 --check value eq b --save F12.lists/rpop
  control-automation api GET /v1/kv/qa-list/len --query automation_id="$AID" --expect 200 --check length eq 1 --save F12.lists/len
  control-automation api POST /v1/kv/qa-list/rpop --query automation_id="$AID" --expect 200 --check value eq a
  control-automation api POST /v1/kv/qa-list/lpop --query automation_id="$AID" --expect 200 --check value eq null
  control-automation api GET /v1/kv/qa-list --query automation_id="$AID" --expect 200 --check value len-eq 0
  control-automation api POST /v1/kv/qa-list-missing/rpop --query automation_id="$AID" --expect 200 --check value eq null
  control-automation api GET /v1/kv/qa-list-missing/len --query automation_id="$AID" --expect 404 --check detail eq key_not_found
  control-automation api PUT /v1/kv/qa-list-obj --query automation_id="$AID" --json '{"x":1}' --expect 201
  control-automation api POST /v1/kv/qa-list-obj/rpush --query automation_id="$AID" --json '{"value":1}' --expect 400 --check detail eq 'type_mismatch: value is not a list'
  control-automation api POST /v1/kv/qa-list-obj/lpop --query automation_id="$AID" --expect 400 --check detail eq 'type_mismatch: value is not a list'
  control-automation api GET /v1/kv/qa-list-obj/len --query automation_id="$AID" --expect 400 --check detail eq 'type_mismatch: value is not a list'
  ```
  Popping the last element leaves an empty list stored, not a deleted key.
- **DELETE (`F12.delete`).** An existing key is removed; a missing one is a 200 no-op.
  ```sh
  control-automation api PUT /v1/kv/qa-del --query automation_id="$AID" --json '"bye"' --expect 201
  control-automation api DELETE /v1/kv/qa-del --query automation_id="$AID" --expect 200 --check deleted eq true --save F12.delete/delete
  control-automation api GET /v1/kv/qa-del --query automation_id="$AID" --expect 404 --check detail eq key_not_found
  control-automation api GET /v1/kv --query automation_id="$AID" --query limit=1000 --expect 200 --check keys not-contains qa-del
  control-automation api DELETE /v1/kv/qa-del --query automation_id="$AID" --expect 200 --check deleted eq false --save F12.delete/missing
  ```
- **Batch (`F12.batch`).** Mixed ops in one call; results carry op-specific fields; state matches.
  ```sh
  control-automation api PUT /v1/kv/qa-b-old --query automation_id="$AID" --json '"x"' --expect 201
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"set","key":"qa-b-cfg","value":{"a":1}},{"op":"incr","key":"qa-b-cnt","by":3},{"op":"decr","key":"qa-b-cnt"},{"op":"rpush","key":"qa-b-q","value":"j1"},{"op":"lpush","key":"qa-b-q","value":"j0"},{"op":"rpop","key":"qa-b-q"},{"op":"patch","key":"qa-b-cfg","path":"b.c","value":2},{"op":"delete","key":"qa-b-old"}]}' --expect 200 --check version ge 1 --check results len-eq 8 --check results.0.created eq true --check results.1.value eq 3 --check results.2.value eq 2 --check results.4.length eq 2 --check results.5.value eq j1 --check results.6.success eq true --check results.7.deleted eq true --save F12.batch/batch
  control-automation api GET /v1/kv/qa-b-cfg --query automation_id="$AID" --expect 200 --check value.a eq 1 --check value.b.c eq 2
  control-automation api GET /v1/kv/qa-b-cnt --query automation_id="$AID" --expect 200 --check value eq 2
  control-automation api GET /v1/kv/qa-b-q --query automation_id="$AID" --expect 200 --check value len-eq 1 --check value.0 eq j0
  control-automation api GET /v1/kv/qa-b-old --query automation_id="$AID" --expect 404
  ```
- **Batch atomicity (`F12.batch-atomic`).** The third op fails, so the first two are rolled back.
  ```sh
  control-automation api PUT /v1/kv/qa-ba-list --query automation_id="$AID" --json '[1]' --expect 201
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"set","key":"qa-ba-new","value":1},{"op":"rpush","key":"qa-ba-list","value":2},{"op":"incr","key":"qa-ba-list"}]}' --expect 400 --check detail.error eq operation_failed --check detail.operation_index eq 2 --check detail.operation.op eq incr --check detail.message eq "key 'qa-ba-list' is not an integer" --save F12.batch-atomic/failed
  control-automation api GET /v1/kv/qa-ba-new --query automation_id="$AID" --expect 404
  control-automation api GET /v1/kv/qa-ba-list --query automation_id="$AID" --expect 200 --check value len-eq 1 --save F12.batch-atomic/unchanged
  ```
- **Batch validation (`F12.batch-validation`).** Schema errors are 422; semantic ones are 400 `operation_failed`; batch patch creates its key.
  ```sh
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[]}' --expect 422 --check detail.0.type eq too_short
  OPS101=$(printf '{"op":"incr","key":"qa-bv-many"},%.0s' $(seq 100))
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json "{\"operations\":[${OPS101}{\"op\":\"incr\",\"key\":\"qa-bv-many\"}]}" --expect 422 --quiet --check detail.0.type eq too_long
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json "{\"operations\":[{\"op\":\"set\",\"key\":\"$(printf 'k%.0s' $(seq 256))\",\"value\":1}]}" --expect 422 --quiet
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"set","key":"qa-bv","value":1,"nx":true,"xx":true}]}' --expect 422 --quiet --check detail.0.msg contains 'Cannot use both nx and xx'
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"set","key":"$qa","value":1}]}' --expect 400 --check detail.message eq "keys starting with '\$' are reserved for system use"
  control-automation api PUT /v1/kv/qa-bv-exists --query automation_id="$AID" --json '1' --expect 201
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"set","key":"qa-bv-exists","value":2,"nx":true}]}' --expect 400 --check detail.message eq "key 'qa-bv-exists' already exists (nx=true)"
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"set","key":"qa-bv-absent","value":2,"xx":true}]}' --expect 400 --check detail.message eq "key 'qa-bv-absent' does not exist (xx=true)"
  control-automation api GET /v1/kv/qa-bv-many --query automation_id="$AID" --expect 404
  control-automation api PATCH /v1/kv/qa-bv-patch --query automation_id="$AID" --json '{"path":"a.b","value":1}' --expect 404
  control-automation api POST /v1/kv/batch --query automation_id="$AID" --json '{"operations":[{"op":"patch","key":"qa-bv-patch","path":"a.b","value":1}]}' --expect 200 --check results.0.success eq true --save F12.batch-validation/patch-creates
  control-automation api GET /v1/kv/qa-bv-patch --query automation_id="$AID" --expect 200 --check value.a.b eq 1
  ```
- **Isolation between automations (`F12.isolation`).** A and B each see only their own value under the same key, whichever credential carries the request.
  ```sh
  control-automation api PUT /v1/kv/qa-iso --query automation_id="$AID" --json '"from-a"' --expect 201
  control-automation api GET /v1/kv/qa-iso --query automation_id="$BID" --expect 404 --save F12.isolation/user-b
  control-automation api GET /v1/kv --query automation_id="$BID" --query limit=1000 --expect 200 --check keys not-contains qa-iso
  TOKEN_B=$(control-automation kv token --automation-id "$BID")
  control-automation api GET /v1/kv/qa-iso --auth none --header "Authorization: Bearer $TOKEN_B" --query automation_id="$AID" --expect 404 --save F12.isolation/jwt-b-ignores-query
  control-automation api PUT /v1/kv/qa-iso --auth none --header "Authorization: Bearer $TOKEN_B" --query automation_id="$AID" --json '"from-b"' --expect 201
  control-automation api GET /v1/kv/qa-iso --query automation_id="$AID" --expect 200 --check value eq from-a
  control-automation api GET /v1/kv/qa-iso --query automation_id="$BID" --expect 200 --check value eq from-b
  control-automation api GET /v1/kv/qa-iso --auth none --header "Authorization: Bearer $TOKEN" --expect 200 --check value eq from-a
  ```
  B's token with `?automation_id=$AID` still wrote into B, not A.
- **Encrypted at rest (`F12.encrypted-at-rest`).** The stored column is a Fernet token and the plaintext appears nowhere in the table.
  ```sh
  control-automation api PUT /v1/kv/qa-enc --query automation_id="$AID" --json '{"marker":"qa-plaintext-marker"}' --expect 201
  control-automation state sql "SELECT value_encrypted FROM automation_kv WHERE key = 'qa-enc'" --raw --check rows.0.value_encrypted matches '^gAAAAA'
  control-automation state sql "SELECT count(*) AS n FROM automation_kv WHERE value_encrypted LIKE '%qa-plaintext-marker%'" --check rows.0.n eq 0
  control-automation api GET /v1/kv/qa-enc --query automation_id="$AID" --expect 200 --check value.marker eq qa-plaintext-marker --save F12.encrypted-at-rest/read
  ```
- **Soft-deleted automation (`F12.soft-deleted-automation`).** Deleting an automation through the API hides it but leaves its KV readable.
  ```sh
  DID=$(control-automation fixture automation --kind echo --name qa-kv-deleted --field id)
  control-automation api PUT /v1/kv/qa-keep --query automation_id="$DID" --json '"kept"' --expect 201
  control-automation api DELETE "/v1/$DID" --expect 204
  control-automation api GET "/v1/$DID" --expect 404
  control-automation api GET /v1/kv/qa-keep --query automation_id="$DID" --expect 200 --check value eq kept --save F12.soft-deleted-automation/read
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE deleted_at IS NOT NULL AND name = 'qa-kv-deleted'" --check rows.0.n eq 1
  ```
- **A real run uses the store (`F12.run-uses-kv`).** The `kv` fixture increments `qa-runs` and writes `qa-last-run` from inside the run with `AUTOMATION_KV_TOKEN`; dispatch it twice.
  ```sh
  RAID=$(control-automation fixture automation --kind kv --name qa-kv-run --field id)
  RID1=$(control-automation api POST "/v1/$RAID/dispatch" --expect 201 --field id)
  control-automation run wait "$RAID" --run-id "$RID1" --status COMPLETED --timeout 300
  control-automation api GET /v1/kv/qa-runs --query automation_id="$RAID" --expect 200 --check value eq 1
  RID2=$(control-automation api POST "/v1/$RAID/dispatch" --expect 201 --field id)
  control-automation run wait "$RAID" --run-id "$RID2" --status COMPLETED --timeout 300 --save F12.run-uses-kv/run2
  control-automation api GET /v1/kv/qa-runs --query automation_id="$RAID" --expect 200 --check value eq 2 --save F12.run-uses-kv/counter
  control-automation api GET /v1/kv/qa-last-run --query automation_id="$RAID" --expect 200 --check value.run_id eq "$RID2" --check value.count eq 2 --save F12.run-uses-kv/last-run
  ```
  Both runs complete through their callback; the values could only have
  been written with the run's token, since the fixture holds no user key.
- **Malformed GET path (`F12.get-path-malformed`), known bug.** PATCH maps an unclosed bracket to 400, GET crashes with 500 (`parse_path` raises `ValueError`, GET only catches `KeyError`).
  ```sh
  control-automation api PUT /v1/kv/qa-gpm --query automation_id="$AID" --json '{"a":{"b":1}}' --expect 201
  control-automation api GET /v1/kv/qa-gpm --query automation_id="$AID" --query path=a.b --expect 200 --check value eq 1
  control-automation api GET /v1/kv/qa-gpm --query automation_id="$AID" --query 'path=a[b' --expect 400 --save F12.get-path-malformed/get  # bug: status eq 500
  ```
- **Empty PATCH path (`F12.patch-empty-path`), known bug.** `path:""` reaches `set_nested_value`, whose `parts[-1]` raises `IndexError`: 500 instead of 400.
  ```sh
  control-automation api PUT /v1/kv/qa-pep --query automation_id="$AID" --json '{"a":1}' --expect 201
  control-automation api PATCH /v1/kv/qa-pep --query automation_id="$AID" --json '{"path":"a","value":2}' --expect 200
  control-automation api PATCH /v1/kv/qa-pep --query automation_id="$AID" --json '{"path":"","value":1}' --expect 400 --save F12.patch-empty-path/patch  # bug: status eq 500
  ```
- **JWT for an automation that does not exist (`F12.jwt-unknown-automation`), known bug.** The token is accepted (the claim is never checked against the database): reads are empty, and the first write fails the foreign key on `automation_kv_meta`, which surfaces as 500.
  ```sh
  GHOST=$(control-automation kv token --automation-id 00000000-0000-4000-8000-00000000dead)
  control-automation api GET /v1/kv --auth none --header "Authorization: Bearer $GHOST" --expect 200 --check total eq 0
  control-automation api GET /v1/kv/qa-ghost --auth none --header "Authorization: Bearer $GHOST" --expect 404 --check detail eq key_not_found
  control-automation api PUT /v1/kv/qa-ghost --auth none --header "Authorization: Bearer $GHOST" --json '1' --expect 4xx --save F12.jwt-unknown-automation/put  # bug: status eq 500
  ```

## Gotchas

- The 503 check runs before auth, so with the secret empty even a valid key
  or token gets 503, and `kvStore` disappears from capabilities. `restart
  --env AUTOMATION_KV_SECRET=` really sets it empty; `--reset-env` restores
  the run's secret, and the data written before is readable again. Changing
  the secret (not emptying it) makes every stored value fail to decrypt
  (500 `Failed to decrypt value`) and invalidates every issued token.
- A wrong user key gets the KV-specific 401 (`Authentication required:
  provide a KV token or user credentials`), not `Invalid API key`: KV swallows
  the user-auth 401 and reports its own.
- Any Bearer value with exactly two dots is judged as a KV JWT and never falls
  back to user auth. A token's `automation_id` claim wins over the query
  parameter, and the service does not check that the automation exists or
  is not deleted (see `F12.jwt-unknown-automation`); `run_id` is not checked
  either. `docs/kv-store-design.md` mentions a 403 `token_expired`; the code
  answers 401 `Invalid or expired KV token`.
- The `nx` conflict body is a normal response (`{key, created, error}`) with
  no `detail`; every other 409 is an `HTTPException` with `detail`. The
  version-mismatch 409 carries `Retry-After: 1`; the PostgreSQL-only
  `kv_store_busy` lock-timeout 409 cannot be produced on a SQLite run.
- The version is one counter per automation, not per key, and reads 0
  before anything was written for the automation (no meta row yet).
- PUT cannot store JSON `null` (422 `Field required`); use a batch `set`.
  The 422 for `null` and the 500s in the three known-bug bullets are
  candidates for fixes, not contracts.
- Nesting depth counts containers: 32 nested lists around a scalar are
  accepted, 33 are refused. NaN is accepted by the JSON parser and refused
  only when the value is serialized for storage (400).
- The size limit measures the compact JSON of one value (quotes included),
  never the automation's total; there is no key-count quota. List pushes,
  incr, patch and batch results are measured after the operation.
- A single PATCH needs an existing object value (404 otherwise); a batch
  `patch` creates `{}` first. Popping the last list element stores `[]`.
- Keys are one URL path segment: a `/` in a key routes to 404, and percent
  escapes are decoded before validation (`%20%20` is whitespace-only,
  `%01` a control character). `GET /v1/kv/batch` reads a key named `batch`;
  POST to it is the batch endpoint.
- Deleting an automation through the API is a soft delete, so its KV stays
  readable and writable (`F12.soft-deleted-automation`); only a hard delete
  of the row cascades to `automation_kv` and `automation_kv_meta`.
- There is no TTL (`?ex=` in the design doc is future work).

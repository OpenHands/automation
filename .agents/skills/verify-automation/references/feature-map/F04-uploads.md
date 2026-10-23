# Tarball uploads

How a client ships its own code to the service before creating an automation:
it POSTs the raw tarball bytes (not multipart) with a `name` query parameter,
gets back an upload record whose `tarball_path` is
`oh-internal://uploads/<id>`, and passes that path to `POST /v1` (F05). The
service checks the Content-Type, enforces a hard-coded 1 MiB limit twice (a
declared `Content-Length` is refused with 413 before anything is stored; a
body that grows past the limit while streaming is stored as a `FAILED`
record, still answered 201), and never looks inside the bytes. Uploads are
listed, read and soft-deleted per user and org; a deleted or failed upload
can no longer back a new automation.

Source: `openhands/automation/uploads.py`, `openhands/automation/utils/tarball_validation.py`, `openhands/automation/storage/`, `openhands/automation/models.py`

Routes: `POST /api/automation/v1/uploads`, `GET /api/automation/v1/uploads`,
`GET /api/automation/v1/uploads/{upload_id}`,
`DELETE /api/automation/v1/uploads/{upload_id}`

## Sub-features

- `F04.upload`: a raw gzip body with `name` (and optional `description`) answers 201 `COMPLETED` with `size_bytes` equal to the body length and `tarball_path == "oh-internal://uploads/<id>"`; the bytes land in the file store and the row in `tarball_uploads`.
- `F04.content-type`: a Content-Type outside the five tarball types answers 415 with the sorted allowed list; parameters after `;` are ignored and `application/octet-stream` is accepted.
- `F04.name-required`: a missing `name`, an empty one, or one over 255 characters answers 422 and stores nothing.
- `F04.content-not-validated`: any bytes with an allowed Content-Type are stored as `COMPLETED`; the service does not check that the body is a tar.
- `F04.size-declared`: a declared `Content-Length` over 1048576 answers 413 `File too large. Maximum size is 1048576 bytes` and stores no row; exactly 1048576 bytes is accepted.
- `F04.size-streamed`: a chunked body (no `Content-Length`) over 1 MiB answers 201 with `status: FAILED`, `error_message` `File size exceeds limit of 1048576 bytes`, `tarball_path: null`, and leaves no file in storage.
- `F04.bad-content-length`: a non-numeric `Content-Length` is refused by the HTTP server with a plain-text 400 before it reaches the handler (not the 500 the handler's `int()` would raise).
- `F04.list`: `GET /v1/uploads` answers `{uploads, total}` newest first, filters by `status`, pages with `limit`/`offset`, and rejects `limit` 0 or 101 and an unknown status with 422.
- `F04.get`: `GET /v1/uploads/{id}` answers the record; an unknown UUID answers 404 `Upload not found`, a non-UUID 422.
- `F04.delete`: `DELETE` answers 204, removes the stored file and soft-deletes the row; afterwards GET and a second DELETE answer 404 and the list drops it.
- `F04.create-rejects-unusable`: `POST /v1` with a deleted upload's path answers 400 `Upload has been deleted`, with a `FAILED` upload's path 400 `Upload is not ready (status: FAILED)`, and with an unknown upload id 404 `Upload not found`.
- `F04.delete-in-use`: an upload an automation references can still be deleted (204, no in-use guard); the automation keeps its `tarball_path` and its tarball download then answers 404.
- `F04.auth`: every upload route answers 401 without a key.

## How to get to it (agent POV)

- REST: the four routes above. The body is the raw tarball; `name` and
  `description` travel as query parameters so the body can stream.
- CLI: `control-automation api POST /v1/uploads --query name=qa --data-file X --content-type application/gzip`;
  `control-automation fixture tarball --kind echo --upload --field tarball_path`
  builds and uploads a stdlib fixture in one step (every recipe here except
  the raw byte cases uses it).
- The `automation` skill in `OpenHands/extensions` and Agent Canvas upload a
  tarball this way, then create the automation with the returned
  `tarball_path`.
- Preset routes (F06) and draft dispatch create upload rows internally
  (names `prompt-automation-...`, `plugin-automation-...`); they show up in
  this list too.
- Config: `FILE_STORE` (`local` in a local run), `LOCAL_STORAGE_PATH`; a run
  launched by `control-automation` stores files under
  `$AUTOMATION_VERIFY_RUN/storage/automation/uploads/<org>/<user>/<id>.tar`.
  `MAX_UPLOAD_SIZE` in `config.py` is unused: the limit is the constant in
  `uploads.py`.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.
- `fixture tarball --kind echo --field path` builds a ~1 KB gzip fixture
  inside the run directory; each bullet builds its own.

- **Upload a tarball (`F04.upload`).** Raw gzip body; the record, the row and the stored file agree.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  SIZE=$(wc -c < "$TARF" | tr -d ' ')
  UPID=$(control-automation api POST /v1/uploads --query name=qa-upload --query description=qa-desc --data-file "$TARF" --content-type application/gzip --expect 201 --check status eq COMPLETED --check size_bytes eq "$SIZE" --check error_message eq null --save F04.upload/create --field id)
  control-automation api GET "/v1/uploads/$UPID" --expect 200 --check name eq qa-upload --check description eq qa-desc --check tarball_path eq "oh-internal://uploads/$UPID" --check size_bytes eq "$SIZE" --save F04.upload/get
  SP=$(control-automation state sql "SELECT storage_path FROM tarball_uploads WHERE id = replace('$UPID','-','')" --check rows.0.storage_path matches '^uploads/' --field rows.0.storage_path)
  test -f "$AUTOMATION_VERIFY_RUN/storage/automation/$SP"
  cmp "$TARF" "$AUTOMATION_VERIFY_RUN/storage/automation/$SP"
  ```
  The stored object is byte-identical to the body. SQLite keeps the UUID as
  32 hex digits, hence the `replace()`.
- **Content-Type rules (`F04.content-type`).** Wrong types are refused with the allowed list; parameters and the generic binary type pass.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  control-automation api POST /v1/uploads --query name=qa-ct-text --data-file "$TARF" --content-type text/plain --expect 415 --check detail eq 'Content-Type must be one of: application/gzip, application/octet-stream, application/x-compressed-tar, application/x-gzip, application/x-tar' --save F04.content-type/text-plain
  control-automation api POST /v1/uploads --query name=qa-ct-multipart --data-file "$TARF" --content-type multipart/form-data --expect 415
  control-automation api POST /v1/uploads --query name=qa-ct-param --data-file "$TARF" --content-type 'application/gzip; charset=binary' --expect 201 --check status eq COMPLETED
  control-automation api POST /v1/uploads --query name=qa-ct-octet --data-file "$TARF" --content-type application/octet-stream --expect 201 --check status eq COMPLETED
  control-automation state sql "SELECT count(*) AS n FROM tarball_uploads WHERE name LIKE 'qa-ct-%'" --check rows.0.n eq 2
  ```
  Only the two accepted uploads left a row.
- **Required name (`F04.name-required`).** The query parameter is validated before anything is stored.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  N0=$(control-automation state sql "SELECT count(*) AS n FROM tarball_uploads" --field rows.0.n)
  control-automation api POST /v1/uploads --data-file "$TARF" --content-type application/gzip --expect 422 --check detail.0.type eq missing --check detail.0.loc.1 eq name --save F04.name-required/missing
  control-automation api POST /v1/uploads --query name= --data-file "$TARF" --content-type application/gzip --expect 422 --check detail.0.type eq string_too_short
  control-automation api POST /v1/uploads --query "name=$(printf 'q%.0s' $(seq 256))" --data-file "$TARF" --content-type application/gzip --expect 422 --check detail.0.type eq string_too_long
  control-automation state sql "SELECT count(*) AS n FROM tarball_uploads" --check rows.0.n eq "$N0"
  ```
- **Content is not inspected (`F04.content-not-validated`).** Plain text with a tarball Content-Type is stored as completed.
  ```sh
  control-automation api POST /v1/uploads --query name=qa-not-a-tar --data 'this is not a tarball' --content-type application/x-tar --expect 201 --check status eq COMPLETED --check size_bytes eq 21 --check tarball_path matches '^oh-internal://uploads/' --save F04.content-not-validated/text
  ```
  The failure surfaces only when a run tries to unpack it (F07).
- **Declared size limit (`F04.size-declared`).** One byte over the limit is refused up front; exactly the limit is stored.
  ```sh
  head -c 1048577 /dev/zero > "$AUTOMATION_VERIFY_RUN/qa-over.bin"
  head -c 1048576 /dev/zero > "$AUTOMATION_VERIFY_RUN/qa-limit.bin"
  control-automation api POST /v1/uploads --query name=qa-over --data-file "$AUTOMATION_VERIFY_RUN/qa-over.bin" --content-type application/gzip --expect 413 --check detail eq 'File too large. Maximum size is 1048576 bytes' --save F04.size-declared/over
  control-automation state sql "SELECT count(*) AS n FROM tarball_uploads WHERE name = 'qa-over'" --check rows.0.n eq 0
  control-automation api POST /v1/uploads --query name=qa-limit --data-file "$AUTOMATION_VERIFY_RUN/qa-limit.bin" --content-type application/gzip --expect 201 --check status eq COMPLETED --check size_bytes eq 1048576 --quiet --save F04.size-declared/limit
  ```
  The CLI sends `Content-Length`, so a 413 here is the header check, not the
  streaming check.
- **Streamed size limit (`F04.size-streamed`).** A chunked body with no `Content-Length` is cut off while streaming and recorded as failed.
  ```sh
  head -c 1100000 /dev/zero > "$AUTOMATION_VERIFY_RUN/qa-chunked.bin"
  control-automation exec --expect-output '"status":"FAILED"' --save F04.size-streamed/chunked -- sh -c 'curl -sS -X POST -H "X-Session-API-Key: $AUTOMATION_API_KEY" -H "Content-Type: application/gzip" -H "Transfer-Encoding: chunked" --data-binary @"$1" -w " HTTP %{http_code}" "$AUTOMATION_URL/api/automation/v1/uploads?name=qa-chunked"' sh "$AUTOMATION_VERIFY_RUN/qa-chunked.bin"
  CHID=$(control-automation api GET /v1/uploads --query status=FAILED --expect 200 --check uploads.0.name eq qa-chunked --check uploads.0.error_message eq 'File size exceeds limit of 1048576 bytes' --check uploads.0.tarball_path eq null --check uploads.0.size_bytes gt 1048576 --field uploads.0.id)
  SP=$(control-automation state sql "SELECT status, storage_path FROM tarball_uploads WHERE id = replace('$CHID','-','')" --check rows.0.status eq FAILED --field rows.0.storage_path)
  test ! -e "$AUTOMATION_VERIFY_RUN/storage/automation/$SP"
  ```
  curl prints the 201 body followed by `HTTP 201`. `size_bytes` is the byte
  count at the chunk that crossed the limit, not the body length.
- **Non-numeric Content-Length (`F04.bad-content-length`).** The server's HTTP parser answers 400 before the route runs.
  ```sh
  control-automation exec --expect-output 'HTTP 400' --reject-output 'HTTP 500' --save F04.bad-content-length/abc -- sh -c 'curl -sS -X POST -H "X-Session-API-Key: $AUTOMATION_API_KEY" -H "Content-Type: application/gzip" -H "Content-Length: abc" --data-binary qa -w " HTTP %{http_code}" "$AUTOMATION_URL/api/automation/v1/uploads?name=qa-cl-abc"'
  control-automation state sql "SELECT count(*) AS n FROM tarball_uploads WHERE name = 'qa-cl-abc'" --check rows.0.n eq 0
  control-automation api GET /health --auth none --expect 200
  ```
  The body is uvicorn's plain-text `Invalid HTTP request received.`; the
  service stays healthy. `api --header 'Content-Length: abc'` cannot carry
  this case because the HTTP client rewrites the header.
- **List, filter and page (`F04.list`).** Newest first, filtered, paged and bounded.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  LA=$(control-automation api POST /v1/uploads --query name=qa-list-a --data-file "$TARF" --content-type application/gzip --expect 201 --field id)
  sleep 1.2
  LB=$(control-automation api POST /v1/uploads --query name=qa-list-b --data-file "$TARF" --content-type application/gzip --expect 201 --field id)
  TOTAL=$(control-automation api GET /v1/uploads --expect 200 --check uploads.0.id eq "$LB" --check uploads.1.id eq "$LA" --field total)
  control-automation api GET /v1/uploads --query limit=1 --query offset=1 --expect 200 --check uploads len-eq 1 --check uploads.0.id eq "$LA" --check total eq "$TOTAL" --save F04.list/page
  control-automation api GET /v1/uploads --query status=COMPLETED --expect 200 --check uploads.0.id eq "$LB" --quiet
  control-automation api GET /v1/uploads --query status=UPLOADING --expect 200 --check total eq 0 --check uploads len-eq 0
  control-automation api GET /v1/uploads --query limit=0 --expect 422 --check detail.0.loc.1 eq limit
  control-automation api GET /v1/uploads --query limit=101 --expect 422
  control-automation api GET /v1/uploads --query status=DONE --expect 422 --check detail.0.loc.1 eq status
  ```
  `total` counts every live upload of the caller regardless of paging.
  `F04.size-streamed` shows the `status=FAILED` filter. The `sleep` is
  deliberate: `created_at` is stored with one-second precision and the list
  has no tie-breaker, so two uploads in the same second come back in either
  order.
- **Get one upload (`F04.get`).** By id, unknown id, and a malformed id.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  GID=$(control-automation api POST /v1/uploads --query name=qa-get --data-file "$TARF" --content-type application/gzip --expect 201 --field id)
  control-automation api GET "/v1/uploads/$GID" --expect 200 --check id eq "$GID" --check status eq COMPLETED --check created_at exists --check updated_at exists
  control-automation api GET /v1/uploads/00000000-0000-4000-8000-000000000000 --expect 404 --check detail eq 'Upload not found' --save F04.get/unknown
  control-automation api GET /v1/uploads/not-a-uuid --expect 422 --check detail.0.loc.1 eq upload_id
  ```
- **Delete an upload (`F04.delete`).** Soft delete plus storage removal; every second view agrees.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  DID=$(control-automation api POST /v1/uploads --query name=qa-delete --data-file "$TARF" --content-type application/gzip --expect 201 --field id)
  SP=$(control-automation state sql "SELECT storage_path FROM tarball_uploads WHERE id = replace('$DID','-','')" --field rows.0.storage_path)
  test -f "$AUTOMATION_VERIFY_RUN/storage/automation/$SP"
  control-automation api DELETE "/v1/uploads/$DID" --expect 204 --save F04.delete/delete
  control-automation api GET "/v1/uploads/$DID" --expect 404 --check detail eq 'Upload not found'
  control-automation api GET /v1/uploads --query limit=100 --expect 200 --check uploads not-contains "$DID" --quiet
  control-automation api DELETE "/v1/uploads/$DID" --expect 404
  control-automation state sql "SELECT deleted_at FROM tarball_uploads WHERE id = replace('$DID','-','')" --check rows.0.deleted_at ne null
  test ! -e "$AUTOMATION_VERIFY_RUN/storage/automation/$SP"
  ```
- **Unusable uploads cannot back an automation (`F04.create-rejects-unusable`).** Deleted, failed and unknown uploads are refused by `POST /v1`.
  ```sh
  TARF=$(control-automation fixture tarball --kind echo --field path)
  XID=$(control-automation api POST /v1/uploads --query name=qa-gone --data-file "$TARF" --content-type application/gzip --expect 201 --field id)
  control-automation api DELETE "/v1/uploads/$XID" --expect 204
  control-automation api POST /v1 --json "{\"name\":\"qa-from-deleted\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"oh-internal://uploads/$XID\",\"entrypoint\":\"bash run.sh\"}" --expect 400 --check detail eq 'Upload has been deleted' --save F04.create-rejects-unusable/deleted
  head -c 1100000 /dev/zero > "$AUTOMATION_VERIFY_RUN/qa-chunked2.bin"
  control-automation exec --expect-output '"status":"FAILED"' -- sh -c 'curl -sS -X POST -H "X-Session-API-Key: $AUTOMATION_API_KEY" -H "Content-Type: application/gzip" -H "Transfer-Encoding: chunked" --data-binary @"$1" "$AUTOMATION_URL/api/automation/v1/uploads?name=qa-failed-src"' sh "$AUTOMATION_VERIFY_RUN/qa-chunked2.bin"
  FID=$(control-automation api GET /v1/uploads --query status=FAILED --expect 200 --check uploads.0.name eq qa-failed-src --field uploads.0.id)
  control-automation api POST /v1 --json "{\"name\":\"qa-from-failed\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"oh-internal://uploads/$FID\",\"entrypoint\":\"bash run.sh\"}" --expect 400 --check detail eq 'Upload is not ready (status: FAILED)' --save F04.create-rejects-unusable/failed
  control-automation api POST /v1 --json '{"name":"qa-from-unknown","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"oh-internal://uploads/00000000-0000-4000-8000-000000000000","entrypoint":"bash run.sh"}' --expect 404 --check detail eq 'Upload not found'
  control-automation api GET /v1 --query limit=100 --expect 200 --check automations not-contains qa-from- --quiet
  ```
  None of the three created an automation. The internal URL must carry the
  dashed UUID: the 32-hex form SQLite stores answers 422
  `Invalid internal upload URL format...`.
- **No in-use guard (`F04.delete-in-use`).** Deleting an upload an automation uses succeeds and breaks the automation's tarball.
  ```sh
  TP=$(control-automation fixture tarball --kind echo --upload --field tarball_path)
  IUID=${TP#oh-internal://uploads/}
  IAID=$(control-automation api POST /v1 --json "{\"name\":\"qa-in-use\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\"}" --expect 201 --field id)
  control-automation api GET "/v1/$IAID/tarball" --expect 200 --quiet
  control-automation api DELETE "/v1/uploads/$IUID" --expect 204 --save F04.delete-in-use/delete
  control-automation api GET "/v1/$IAID" --expect 200 --check tarball_path eq "$TP" --check state eq ACTIVE
  control-automation api GET "/v1/$IAID/tarball" --expect 404 --check detail eq 'Tarball not found (underlying upload may have been deleted)' --save F04.delete-in-use/tarball
  ```
  The automation stays `ACTIVE`; its next run fails at tarball download.
- **Key required (`F04.auth`).** No key, no upload routes.
  ```sh
  control-automation api POST /v1/uploads --auth none --query name=qa-noauth --data qa --content-type application/gzip --expect 401 --save F04.auth/post
  control-automation api GET /v1/uploads --auth none --expect 401
  control-automation api GET /v1/uploads/00000000-0000-4000-8000-000000000000 --auth bad --expect 401
  control-automation api DELETE /v1/uploads/00000000-0000-4000-8000-000000000000 --auth none --expect 401
  control-automation state sql "SELECT count(*) AS n FROM tarball_uploads WHERE name = 'qa-noauth'" --check rows.0.n eq 0
  ```

## Gotchas

- The response is 201 even when the upload failed while streaming; read
  `status` (`COMPLETED`/`FAILED`), not the status code. Only `COMPLETED`
  records carry a `tarball_path`.
- `api --data-file` always sends `Content-Length`, so it can only reach the
  413 header path. The streamed path needs a chunked body: `curl -H 'Transfer-Encoding: chunked'`
  inside `exec`.
- The body is never inspected. A non-tar upload is `COMPLETED` and fails
  only when a run downloads and unpacks it.
- Uploads are scoped per user and org: another user in the same org gets 404
  from GET and DELETE and 403 `Upload belongs to another user` when creating
  an automation from it (cloud mode only; the local user is the only user, so
  this cannot be driven here). Deleting needs only `view_automations`, like
  uploading.
- Uploads created by presets and draft dispatch appear in `GET /v1/uploads`
  next to your own (`prompt-automation-<name>`, `plugin-automation-<name>`,
  `...-edit` after a prompt PATCH).
- `created_at` has one-second precision (no fraction, unlike `updated_at`)
  and the list orders by it alone, so uploads created in the same second
  have no stable order across pages.
- SQLite stores ids as 32 hex digits without dashes; `state sql` queries
  compare with `replace(id_with_dashes, '-', '')`.
- There is no in-use check on delete and no garbage collection of
  unreferenced uploads (template repeats and prompt edits leave some behind).

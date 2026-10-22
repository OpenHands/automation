# Git sync

An org admin mirrors the org's automations to a git repository and back:
`PUT /config` names the repo (setting a repo URL is what turns sync on),
`POST /check` tests a candidate configuration with one `git ls-remote`
without saving it, `POST /sync` starts one bidirectional cycle in the
background (pull, import commits pushed to the repo, export automations
changed through the API, commit and push), and `GET /status` reports the
configuration plus the last cycle's commit, time, error, lease and dirty
count. Every automation create, update and delete marks it dirty, even while
sync is unconfigured, and a dirty automation wins over a conflicting git-side
edit. A background loop (15 s tick) also syncs on `interval_seconds`, which
defaults to 0 (manual only). Recipes here use a local bare repository as the
remote (`fixture git-remote`), which drives the same `git` CLI code path as a
hosted remote, translated from "GitHub" to "a git remote".

Source: `openhands/automation/git_sync/`, `openhands/automation/config.py`, `openhands/automation/models.py`

Launch: `--env AUTOMATION_GIT_SYNC_GIT_TIMEOUT_SECONDS=8`

Routes: `GET /api/automation/v1/git-sync/status`, `PUT /api/automation/v1/git-sync/config`,
`POST /api/automation/v1/git-sync/check`, `POST /api/automation/v1/git-sync/sync`

## Sub-features

- `F13.status-defaults`: with nothing configured, `GET /status` reports `enabled:false`, `repo_url:""`, branch `main`, path `automations`, interval 0, no encryption, no last sync and no lease; all four routes answer 401 without a key.
- `F13.sync-disabled`: `POST /sync` answers 503 `Git sync is not enabled` while no repo URL is configured.
- `F13.dirty-unconfigured`: creating an automation while sync is unconfigured still creates a dirty state row with a slug, and `dirty_count` counts it.
- `F13.check-no-url`: `POST /check {}` with nothing configured answers 200 `{ok:false, detail:"No repository URL is configured."}`.
- `F13.check-empty-repo`: `POST /check` on an empty bare repo answers `{ok:true, branch_exists:false}` and saves nothing.
- `F13.check-bad-remote`: `POST /check` on a path that is not a repository answers 200 `{ok:false}` with git's failure (`git command failed (128): git ls-remote --heads -- <url> main`).
- `F13.check-redaction`: credentials embedded in the URL come back as `***` in the check detail, and neither they nor the token appear.
- `F13.config-enable`: `PUT /config {"repo_url":...}` enables sync without a restart, `GET /status` reads it back, and the caller is stored as the configuring user.
- `F13.repo-conflict`: `PUT /config` answers 409 when another org already syncs the same repository, branch and path (URL compared case-insensitively, without trailing `/` or `.git`).
- `F13.config-partial`: an omitted field is unchanged, `null` reverts a field to its default, and a blank string counts as `null`.
- `F13.config-validation`: `path` is normalized (slashes, `.` segments stripped; blank reverts) and `..`, `/` or a negative or non-integer `interval_seconds` answer 422 without changing anything; unknown fields are ignored.
- `F13.config-pause`: `enabled:false` pauses sync (status `enabled:false`, `POST /sync` 503) and `enabled:true` resumes it.
- `F13.secrets-at-rest`: a token set through `PUT /config` never appears in the status body and is stored as a Fernet token (`gAAAAA...`) in the org's overrides JSON; `null` removes it.
- `F13.sync-first`: `POST /sync` answers 202 `{triggered:true}`; the cycle pushes the branch to the empty remote, status records `last_synced_commit` equal to the remote's `main` and clears `dirty_count`, `/check` then reports `branch_exists:true`, and the checkout lives in the run's `workspaces/git-sync`.
- `F13.export-layout`: each automation is exported as `automations/<slug>/automation.yaml` plus its tarball extracted under `tarball/`, with `tarball_source.type: internal` and executable members listed in `tarball_executables`.
- `F13.commit-author`: commits use the message `Sync automations from agent server` and author `OpenHands Automation <automation@openhands.dev>` unless `author_name`/`author_email` are set.
- `F13.slugs`: slugs are the lowercased name with non-alphanumeric runs collapsed to `-`, capped at 80 characters; a second automation with the same name gets `-<first 8 hex of its id>`.
- `F13.export-external`: an automation with an external `tarball_path` exports only `automation.yaml` with `tarball_source: {type: external, url: ...}`.
- `F13.import-new`: a directory pushed to the repo with `automation.yaml` and `tarball/` becomes an automation owned by the configuring user, with an internal tarball, and the import pushes no commit of its own.
- `F13.import-invalid-skipped`: a directory with invalid YAML or without `entrypoint` is skipped with a logged warning; the cycle still succeeds.
- `F13.import-update`: editing `name` and `enabled` in a synced `automation.yaml` updates the automation; its slug directory stays.
- `F13.conflict-service-wins`: when the same automation is edited through the API and in git before a sync, the API version ends up in both the database and the repo.
- `F13.import-delete`: removing a synced directory in git soft-deletes the automation (404 afterwards) and drops its state row.
- `F13.api-delete-export`: deleting an automation through the API removes its generated files from the repo on the next sync, while a README committed next to them stays.
- `F13.encryption`: setting `encryption_key` marks every automation dirty, stores the key encrypted, and the next sync rewrites every generated file as a Fernet token; clearing it rewrites them as plaintext.
- `F13.path-change`: changing `path` re-exports every automation under the new path and leaves the old directories in place.
- `F13.error-recorded`: a cycle against a missing repository leaves `last_error` (git's failure) and `last_error_at` while `last_synced_at` stays put; the next successful cycle clears both.
- `F13.lease`: while a cycle runs, status reports `sync_in_progress:true` with `sync_started_at`, and a second `POST /sync` answers 202 `{triggered:false}`; a hung remote ends with a `timed out` error after the git timeout.
- `F13.interval`: a positive `interval_seconds` makes the background loop sync with no manual trigger within one 15 s tick plus the interval; setting it back to 0 stops automatic syncs.

## How to get to it (agent POV)

- REST: the four routes above, under the service base path
  (`/api/automation/v1/git-sync/...`). `GET /status` needs
  `view_automations`; the other three need `manage_automations`. The local
  key carries both, so the 403 path is not reachable in local mode. Every
  route acts on the caller's org (`user.org_id`); local mode has exactly one.
- Agent Canvas: the Git Sync settings page (in `OpenHands/OpenHands`) calls
  `PUT /config` on save, `POST /check` for "Test connection", `POST /sync`
  for "Sync now" and polls `GET /status`. No SDK or TypeScript client method
  in this repository reaches these routes.
- Git: anything pushed to the configured branch under `{path}/{slug}/` is
  an entry point too; the next cycle imports it as an automation that runs as
  the configuring user.
- Config: `AUTOMATION_GIT_SYNC_*` env defaults (`config.py`
  `GitSyncSettings`), merged under the org's overrides. This family uses
  `AUTOMATION_GIT_SYNC_GIT_TIMEOUT_SECONDS=8` (Launch line) so the hung-remote
  recipe finishes quickly. The checkout directory is
  `AUTOMATION_GIT_SYNC_LOCAL_WORKDIR`, else
  `$AUTOMATION_WORKSPACE_BASE/git-sync`; `launch` sets the workspace base to
  the run's own `workspaces/`, so the default is already private to the run
  and these recipes do not set the workdir. Stored secrets are wrapped with
  `AUTOMATION_GIT_SYNC_SECRET`, else `AUTOMATION_KV_SECRET` (which `launch`
  sets), else a key file under the workspace (local mode only).
- Recipes drive everything through REST, push git-side changes from the
  fixture's work clone, and read results from the bare repo
  (`git -C "$B" show/ls-tree/log`), `GET /v1/{id}`, the state table
  (`state sql`) and the service log (`logs --grep`).

## Driving it with control-automation

Preconditions:

- A fresh run with this family's Launch flags (`map run --file F13 --fresh`).
  No LLM. `git` on `PATH`.
- The bare remote and its work clone, created right away so the
  check recipes can use the still-empty repo. Commits in the clone pass
  `-c user.name=... -c user.email=...` so no global identity is needed.
  ```sh
  URL=$(control-automation fixture git-remote --name sync --field url)
  B=$(control-automation fixture git-remote --name sync --field bare)
  W=$(control-automation fixture git-remote --name sync --field work)
  test -d "$B" && test -d "$W/.git"
  ```

- **Defaults and auth (`F13.status-defaults`).** Nothing configured yet; no key, no access.
  ```sh
  control-automation api GET /v1/git-sync/status --expect 200 --check enabled eq false --check repo_url eq '' --check branch eq main --check path eq automations --check interval_seconds eq 0 --check encryption_enabled eq false --check last_synced_commit eq null --check last_synced_at eq null --check last_error eq null --check dirty_count eq 0 --check sync_in_progress eq false --check sync_started_at eq null --save F13.status-defaults/status
  control-automation api GET /v1/git-sync/status --auth none --expect 401
  control-automation api GET /v1/git-sync/status --auth bad --expect 401 --check detail eq 'Invalid API key'
  control-automation api PUT /v1/git-sync/config --auth none --json '{"repo_url":"file:///qa-nowhere.git"}' --expect 401
  control-automation api POST /v1/git-sync/check --auth none --json '{}' --expect 401
  control-automation api POST /v1/git-sync/sync --auth none --expect 401
  control-automation api GET /v1/git-sync/status --check repo_url eq ''
  ```
  The refused `PUT` left nothing behind: the repo URL is still empty.
- **Sync refused while unconfigured (`F13.sync-disabled`).**
  ```sh
  control-automation api POST /v1/git-sync/sync --expect 503 --check detail eq 'Git sync is not enabled' --save F13.sync-disabled/sync
  ```
- **Dirty marking without sync (`F13.dirty-unconfigured`).** An automation created now is queued for the first export.
  ```sh
  AM=$(control-automation fixture automation --kind echo --name 'My Daily Report!' --field id)
  control-automation api GET /v1/git-sync/status --expect 200 --check enabled eq false --check dirty_count eq 1 --save F13.dirty-unconfigured/status
  control-automation state sql "SELECT slug, dirty FROM automation_git_sync_state" --check rows.0.slug eq my-daily-report --check rows.0.dirty eq 1 --check count eq 1
  ```
- **Check without a URL (`F13.check-no-url`).**
  ```sh
  control-automation api POST /v1/git-sync/check --json '{}' --expect 200 --check ok eq false --check branch_exists eq false --check detail eq 'No repository URL is configured.' --save F13.check-no-url/check
  ```
- **Check an empty repo (`F13.check-empty-repo`).** Reachable, branch not there yet, and nothing saved.
  ```sh
  control-automation api POST /v1/git-sync/check --json "{\"repo_url\":\"$URL\"}" --expect 200 --check ok eq true --check branch_exists eq false --check detail eq null --save F13.check-empty-repo/check
  control-automation api GET /v1/git-sync/status --check repo_url eq '' --check enabled eq false
  ```
- **Check a missing repository (`F13.check-bad-remote`).** A failed check is a 200 with git's own error.
  ```sh
  control-automation api POST /v1/git-sync/check --json '{"repo_url":"/qa-nonexistent/repo.git"}' --expect 200 --check ok eq false --check branch_exists eq false --check detail matches '^git command failed \(128\): git ls-remote --heads -- /qa-nonexistent/repo.git main' --save F13.check-bad-remote/check
  ```
- **Credentials redacted (`F13.check-redaction`).** Port 1 refuses the connection at once.
  ```sh
  control-automation api POST /v1/git-sync/check --json '{"repo_url":"https://qa-user:qa-url-pass@127.0.0.1:1/r.git","token":"qa-check-token-77"}' --expect 200 --check ok eq false --check detail contains 'https://***@127.0.0.1:1/r.git' --check detail not-contains qa-url-pass --check detail not-contains qa-user --check detail not-contains qa-check-token-77 --save F13.check-redaction/check
  ```
- **Enable by setting the repo (`F13.config-enable`).**
  Requires: `F13.dirty-unconfigured`
  ```sh
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"$URL\"}" --expect 200 --check enabled eq true --check repo_url eq "$URL" --check branch eq main --save F13.config-enable/put
  control-automation api GET /v1/git-sync/status --expect 200 --check enabled eq true --check repo_url eq "$URL" --check last_synced_at eq null
  ME=$(control-automation api GET "/v1/$AM" --field user_id)
  control-automation state sql "SELECT configured_by_user_id AS who FROM automation_git_sync_org_config" --check count eq 1 --check rows.0.who eq "$(echo "$ME" | tr -d -)"
  ```
  No cycle runs on save: `last_synced_at` stays null until a sync.
- **Another org already syncs the repo (`F13.repo-conflict`).** Seed a second org's row (local mode has only one org, so it goes straight into the database), then save the same repo.
  ```sh
  control-automation exec -- python3 -c 'import json, os, sqlite3, sys; db = sqlite3.connect(os.environ["AUTOMATION_VERIFY_RUN"] + "/private/automations.db", timeout=10); db.execute("INSERT INTO automation_git_sync_org_config (org_id, overrides, consecutive_auth_failures) VALUES (?, ?, 0)", ("00000000000000000000000000000f13", json.dumps({"git_sync_repo_url": sys.argv[1].upper() + "/"}))); db.commit()' "$URL"
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"${URL%.git}\"}" --expect 409 --check detail contains 'Another organization already syncs this repository, branch and path' --save F13.repo-conflict/put
  control-automation api PUT /v1/git-sync/config --json '{"path":"qa-other-org-free"}' --expect 200 --check path eq qa-other-org-free
  control-automation exec -- python3 -c 'import os, sqlite3; db = sqlite3.connect(os.environ["AUTOMATION_VERIFY_RUN"] + "/private/automations.db", timeout=10); db.execute("DELETE FROM automation_git_sync_org_config WHERE org_id = ?", ("00000000000000000000000000000f13",)); db.commit()'
  control-automation api PUT /v1/git-sync/config --json '{"path":null}' --expect 200 --check path eq automations
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_org_config" --check rows.0.n eq 1
  control-automation api GET /v1/git-sync/status --check repo_url eq "$URL"
  ```
  The other org stores the URL upper-cased with a trailing slash and this one
  sends it without `.git`; both normalize to the same identity, so the save
  is refused and the stored URL is unchanged. A different path does not
  collide. The seeded row is removed before the path goes back to
  `automations` (with it still there, that revert is a 409 too).
- **Partial updates (`F13.config-partial`).** Only the named field changes; `null` and blank revert.
  ```sh
  control-automation api PUT /v1/git-sync/config --json '{"branch":"qa-dev"}' --expect 200 --check branch eq qa-dev --check repo_url eq "$URL" --check path eq automations --save F13.config-partial/branch
  control-automation api GET /v1/git-sync/status --check branch eq qa-dev --check enabled eq true
  control-automation api PUT /v1/git-sync/config --json '{"branch":null}' --expect 200 --check branch eq main
  control-automation api PUT /v1/git-sync/config --json '{"branch":"qa-dev"}' --expect 200 --check branch eq qa-dev
  control-automation api PUT /v1/git-sync/config --json '{"branch":"   "}' --expect 200 --check branch eq main
  control-automation api PUT /v1/git-sync/config --json '{}' --expect 200 --check branch eq main --check repo_url eq "$URL"
  control-automation api GET /v1/git-sync/status --check branch eq main --check repo_url eq "$URL"
  ```
- **Path and interval validation (`F13.config-validation`).**
  ```sh
  control-automation api PUT /v1/git-sync/config --json '{"path":"/qa-autos/"}' --expect 200 --check path eq qa-autos --save F13.config-validation/normalized
  control-automation api PUT /v1/git-sync/config --json '{"path":"qa/./nested//"}' --expect 200 --check path eq qa/nested
  control-automation api PUT /v1/git-sync/config --json '{"path":"../qa-evil"}' --expect 422 --check detail.0.loc.1 eq path --check detail.0.msg contains "must stay inside the repository (no '..' segments)" --save F13.config-validation/traversal
  control-automation api PUT /v1/git-sync/config --json '{"path":"qa\\..\\evil"}' --expect 422 --check detail.0.msg contains 'must stay inside the repository'
  control-automation api PUT /v1/git-sync/config --json '{"path":"/"}' --expect 422 --check detail.0.msg contains 'must not be empty'
  control-automation api PUT /v1/git-sync/config --json '{"interval_seconds":-1}' --expect 422 --check detail.0.loc.1 eq interval_seconds
  control-automation api PUT /v1/git-sync/config --json '{"interval_seconds":"soon"}' --expect 422 --check detail.0.type eq int_parsing
  control-automation api GET /v1/git-sync/status --check path eq qa/nested --check interval_seconds eq 0
  control-automation api PUT /v1/git-sync/config --json '{"path":"","qa_unknown":1}' --expect 200 --check path eq automations
  ```
  The rejected bodies changed nothing (the path is still `qa/nested`); a
  blank path reverts to `automations`, and the unknown field is ignored.
- **Pause and resume (`F13.config-pause`).**
  ```sh
  control-automation api PUT /v1/git-sync/config --json '{"enabled":false}' --expect 200 --check enabled eq false --check repo_url eq "$URL" --save F13.config-pause/pause
  control-automation api POST /v1/git-sync/sync --expect 503 --check detail eq 'Git sync is not enabled'
  control-automation api PUT /v1/git-sync/config --json '{"enabled":true}' --expect 200 --check enabled eq true
  control-automation api GET /v1/git-sync/status --check enabled eq true
  ```
- **Token encrypted at rest (`F13.secrets-at-rest`).** The status body has no token field; the stored blob holds ciphertext only.
  ```sh
  control-automation api PUT /v1/git-sync/config --json '{"token":"qa-git-token-5150"}' --expect 200 --check token missing --check encryption_enabled eq false --save F13.secrets-at-rest/put
  control-automation exec --reject-output qa-git-token-5150 -- sh -c 'curl -s -H "X-Session-API-Key: $AUTOMATION_API_KEY" "$AUTOMATION_API_URL/v1/git-sync/status"'
  control-automation state sql "SELECT json_extract(overrides, '\$.git_sync_token') AS t, instr(overrides, 'qa-git-token-5150') AS leak FROM automation_git_sync_org_config" --raw --check rows.0.t matches '^gAAAAA' --check rows.0.leak eq 0
  control-automation api PUT /v1/git-sync/config --json '{"token":null}' --expect 200
  control-automation state sql "SELECT json_extract(overrides, '\$.git_sync_token') AS t FROM automation_git_sync_org_config" --check rows.0.t eq null
  ```
- **First sync (`F13.sync-first`).** Fire and forget, then poll.
  Requires: `F13.config-enable`
  ```sh
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true --save F13.sync-first/trigger
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at exists --check sync_in_progress eq false --check last_error eq null --check dirty_count eq 0 --save F13.sync-first/status
  HEAD_SHA=$(git -C "$B" rev-parse main)
  control-automation api GET /v1/git-sync/status --check last_synced_commit eq "$HEAD_SHA"
  control-automation api POST /v1/git-sync/check --json '{}' --expect 200 --check ok eq true --check branch_exists eq true
  test "$(git -C "$AUTOMATION_VERIFY_RUN/workspaces/git-sync" remote get-url origin)" = "$URL"
  test "$(git -C "$AUTOMATION_VERIFY_RUN/workspaces/git-sync" rev-parse HEAD)" = "$HEAD_SHA"
  test ! -e "$AUTOMATION_VERIFY_RUN/workspaces/.git-sync-secret-key"
  ```
  The remote had no `main` before; the cycle created it. The checkout is the
  run's `workspaces/git-sync`, and no key file was generated because
  `AUTOMATION_KV_SECRET` wraps the stored secrets.
- **Export layout (`F13.export-layout`).**
  Requires: `F13.sync-first`
  ```sh
  control-automation exec --expect-output automations/my-daily-report/automation.yaml --expect-output automations/my-daily-report/tarball/run.sh -- git -C "$B" ls-tree -r --name-only main
  control-automation exec --save F13.export-layout/yaml --expect-output 'name: My Daily Report!' --expect-output 'entrypoint: bash run.sh' --expect-output 'schedule: 0 0 1 1 *' --expect-output 'type: internal' --expect-output 'state: ACTIVE' --expect-output 'tarball_executables:' --expect-output '- run.sh' -- git -C "$B" show main:automations/my-daily-report/automation.yaml
  control-automation exec --expect-output 'automation.yaml' --expect-output 'tarball' -- git -C "$B" ls-tree --name-only main automations/my-daily-report/
  ```
- **Commit message and author (`F13.commit-author`).**
  Requires: `F13.sync-first`
  ```sh
  test "$(git -C "$B" log -1 --format='%an <%ae>|%s' main)" = 'OpenHands Automation <automation@openhands.dev>|Sync automations from agent server'
  control-automation api PUT /v1/git-sync/config --json '{"author_name":"QA Bot","author_email":"qa-bot@example.invalid"}' --expect 200
  control-automation api PATCH "/v1/$AM" --json '{"timeout":601}' --expect 200
  control-automation api GET /v1/git-sync/status --check dirty_count eq 1
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0
  control-automation exec --save F13.commit-author/log --expect-output 'QA Bot <qa-bot@example.invalid>|Sync automations from agent server' -- git -C "$B" log -1 --format='%an <%ae>|%s' main
  control-automation exec --expect-output 'timeout: 601' -- git -C "$B" show main:automations/my-daily-report/automation.yaml
  control-automation api PUT /v1/git-sync/config --json '{"author_name":null,"author_email":null}' --expect 200
  ```
- **Slugs and collisions (`F13.slugs`).** Two automations with one name, and a 100-character name.
  Requires: `F13.sync-first`
  ```sh
  D1=$(control-automation fixture automation --kind echo --name 'qa dup' --field id)
  D2=$(control-automation fixture automation --kind echo --name 'qa dup' --field id)
  LONG=$(control-automation fixture automation --kind echo --name "QA $(printf 'x%.0s' $(seq 1 97))" --field id)
  control-automation state sql "SELECT slug FROM automation_git_sync_state WHERE automation_id = '$(echo "$D1" | tr -d -)'" --check rows.0.slug eq qa-dup
  control-automation state sql "SELECT slug FROM automation_git_sync_state WHERE automation_id = '$(echo "$D2" | tr -d -)'" --check rows.0.slug eq "qa-dup-$(echo "$D2" | tr -d - | cut -c1-8)"
  control-automation state sql "SELECT slug, length(slug) AS n FROM automation_git_sync_state WHERE automation_id = '$(echo "$LONG" | tr -d -)'" --check rows.0.n eq 80 --check rows.0.slug matches '^qa-x+$'
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0
  git -C "$B" cat-file -e main:automations/qa-dup/automation.yaml
  git -C "$B" cat-file -e "main:automations/qa-dup-$(echo "$D2" | tr -d - | cut -c1-8)/automation.yaml"
  ```
  The slug is fixed when the state row is created (at the API create), so
  the first `qa dup` keeps `qa-dup` and the second gets its id suffix.
- **External tarball (`F13.export-external`).** Only the metadata is exported.
  Requires: `F13.sync-first`
  ```sh
  XT=$(control-automation fixture automation --kind echo --name 'qa external' --extra-json '{"tarball_path":"https://example.com/qa.tar.gz"}' --field id)
  control-automation api GET "/v1/$XT" --check tarball_path eq https://example.com/qa.tar.gz
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0
  control-automation exec --save F13.export-external/yaml --expect-output 'type: external' --expect-output 'url: https://example.com/qa.tar.gz' -- git -C "$B" show main:automations/qa-external/automation.yaml
  test "$(git -C "$B" ls-tree -r --name-only main automations/qa-external)" = automations/qa-external/automation.yaml
  ```
- **Import a new automation from git (`F13.import-new`).**
  Requires: `F13.sync-first`
  ```sh
  git -C "$W" pull -q origin main
  mkdir -p "$W/automations/qa-from-git/tarball"
  printf 'name: qa from git\nentrypoint: bash run.sh\ntrigger: {type: cron, schedule: "0 9 * * 1", timezone: UTC}\n' > "$W/automations/qa-from-git/automation.yaml"
  printf 'echo hi\n' > "$W/automations/qa-from-git/tarball/run.sh"
  git -C "$W" add -A
  git -C "$W" -c user.name=qa -c user.email=qa@example.invalid commit -qm 'qa: add automation from git'
  git -C "$W" push -q origin HEAD:main
  PUSHED=$(git -C "$W" rev-parse HEAD)
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check last_error eq null --check last_synced_commit eq "$PUSHED"
  IMP=$(control-automation state sql "SELECT automation_id FROM automation_git_sync_state WHERE slug = 'qa-from-git'" --field rows.0.automation_id)
  control-automation api GET "/v1/$IMP" --expect 200 --check name eq 'qa from git' --check user_id eq "$ME" --check entrypoint eq 'bash run.sh' --check trigger.schedule eq '0 9 * * 1' --check tarball_path matches '^oh-internal://' --check enabled eq true --save F13.import-new/automation
  control-automation state sql "SELECT dirty FROM automation_git_sync_state WHERE slug = 'qa-from-git'" --check rows.0.dirty eq 0
  test "$(git -C "$B" rev-parse main)" = "$PUSHED"
  ```
  The new automation runs as the configuring user (`$ME`), its tarball was
  rebuilt from `tarball/` into an internal upload, and the cycle pushed
  nothing back (`main` is still the pushed commit).
- **Invalid directories are skipped (`F13.import-invalid-skipped`).**
  Requires: `F13.import-new`
  ```sh
  mkdir -p "$W/automations/qa-bad-yaml" "$W/automations/qa-no-entry"
  printf 'name: [unclosed\n' > "$W/automations/qa-bad-yaml/automation.yaml"
  printf 'name: qa no entry\ntrigger: {type: cron, schedule: "0 9 * * 1"}\ntarball_source: {type: external, url: "https://example.com/qa.tar.gz"}\n' > "$W/automations/qa-no-entry/automation.yaml"
  git -C "$W" add -A
  git -C "$W" -c user.name=qa -c user.email=qa@example.invalid commit -qm 'qa: invalid automations'
  git -C "$W" push -q origin HEAD:main
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check last_error eq null --save F13.import-invalid-skipped/status
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_state WHERE slug IN ('qa-bad-yaml', 'qa-no-entry')" --check rows.0.n eq 0
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name = 'qa no entry'" --check rows.0.n eq 0
  control-automation logs --service automation --grep "Skipping invalid automation directory 'qa-bad-yaml' from git: automation.yaml is not valid YAML" --expect-min 1
  control-automation logs --service automation --grep "Skipping invalid automation directory 'qa-no-entry' from git: automation.yaml missing required 'name' or 'entrypoint'" --expect-min 1
  control-automation api GET "/v1/$IMP" --expect 200 --check name eq 'qa from git'
  ```
- **Update from git (`F13.import-update`).**
  Requires: `F13.import-new`
  ```sh
  printf 'name: qa from git renamed\nenabled: false\nentrypoint: bash run.sh\ntrigger: {type: cron, schedule: "0 9 * * 1", timezone: UTC}\n' > "$W/automations/qa-from-git/automation.yaml"
  git -C "$W" -c user.name=qa -c user.email=qa@example.invalid commit -qam 'qa: rename and disable'
  git -C "$W" push -q origin HEAD:main
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check last_error eq null
  control-automation api GET "/v1/$IMP" --expect 200 --check name eq 'qa from git renamed' --check enabled eq false --check state eq INACTIVE --save F13.import-update/automation
  control-automation state sql "SELECT slug FROM automation_git_sync_state WHERE automation_id = '$IMP'" --check rows.0.slug eq qa-from-git
  ```
- **The service wins a conflict (`F13.conflict-service-wins`).** Edit the same automation in git and through the API, then sync once.
  Requires: `F13.export-layout`
  ```sh
  git -C "$W" pull -q origin main
  sed -i 's/^name: .*/name: qa git side/' "$W/automations/my-daily-report/automation.yaml"
  git -C "$W" -c user.name=qa -c user.email=qa@example.invalid commit -qam 'qa: git-side edit'
  git -C "$W" push -q origin HEAD:main
  control-automation api PATCH "/v1/$AM" --json '{"name":"qa api side"}' --expect 200
  control-automation api GET /v1/git-sync/status --check dirty_count eq 1
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0
  control-automation api GET "/v1/$AM" --check name eq 'qa api side' --save F13.conflict-service-wins/automation
  control-automation exec --expect-output 'name: qa api side' --reject-output 'qa git side' -- git -C "$B" show main:automations/my-daily-report/automation.yaml
  test "$(git -C "$B" log -1 --format=%s main)" = 'Sync automations from agent server'
  ```
  The slug directory keeps its name (`my-daily-report`) after the rename.
- **Delete in git (`F13.import-delete`).**
  Requires: `F13.import-new`
  ```sh
  git -C "$W" pull -q origin main
  git -C "$W" rm -rq automations/qa-from-git
  git -C "$W" -c user.name=qa -c user.email=qa@example.invalid commit -qm 'qa: remove automation'
  git -C "$W" push -q origin HEAD:main
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check last_error eq null
  control-automation api GET "/v1/$IMP" --expect 404 --save F13.import-delete/gone
  control-automation state sql "SELECT deleted_at, enabled, state FROM automations WHERE id = '$IMP'" --check rows.0.deleted_at exists --check rows.0.enabled eq 0 --check rows.0.state eq INACTIVE
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_state WHERE slug = 'qa-from-git'" --check rows.0.n eq 0
  ```
- **Delete through the API (`F13.api-delete-export`).** A user file next to the generated ones survives.
  Requires: `F13.export-layout`
  ```sh
  git -C "$W" pull -q origin main
  printf 'qa notes\n' > "$W/automations/my-daily-report/README.md"
  git -C "$W" add -A
  git -C "$W" -c user.name=qa -c user.email=qa@example.invalid commit -qm 'qa: add readme'
  git -C "$W" push -q origin HEAD:main
  control-automation api DELETE "/v1/$AM" --expect 204
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0
  test "$(git -C "$B" ls-tree -r --name-only main automations/my-daily-report)" = automations/my-daily-report/README.md
  control-automation exec --save F13.api-delete-export/tree --expect-output README.md --reject-output automation.yaml -- git -C "$B" ls-tree -r --name-only main automations/my-daily-report
  ```
- **Encrypted repo files (`F13.encryption`).** Set a key, sync, then clear it and sync again.
  Requires: `F13.slugs`
  ```sh
  N=$(control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_state" --field rows.0.n)
  control-automation api PUT /v1/git-sync/config --json '{"encryption_key":"qa-repo-key-0123456789abcdef"}' --expect 200 --check encryption_enabled eq true --check dirty_count eq "$N" --save F13.encryption/put
  control-automation state sql "SELECT json_extract(overrides, '\$.git_sync_encryption_key') AS k, instr(overrides, 'qa-repo-key-0123456789abcdef') AS leak FROM automation_git_sync_org_config" --raw --check rows.0.k matches '^gAAAAA' --check rows.0.leak eq 0
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0 --check last_error eq null
  for f in $(git -C "$B" ls-tree -r --name-only main automations/qa-dup automations/qa-external); do test "$(git -C "$B" show "main:$f" | head -c 6)" = gAAAAA; done
  control-automation exec --save F13.encryption/ciphertext --expect-output gAAAAA --reject-output 'name: qa dup' -- git -C "$B" show main:automations/qa-dup/automation.yaml
  control-automation api PUT /v1/git-sync/config --json '{"encryption_key":null}' --expect 200 --check encryption_enabled eq false --check dirty_count eq "$N"
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0
  control-automation exec --expect-output 'name: qa dup' --reject-output gAAAAA -- git -C "$B" show main:automations/qa-dup/automation.yaml
  ```
  Every generated file (metadata and tarball members) became a Fernet
  token; clearing the key marked everything dirty again and the next cycle
  wrote plaintext back.
- **Path change (`F13.path-change`).**
  Requires: `F13.slugs`, `F13.export-external`
  ```sh
  control-automation api PUT /v1/git-sync/config --json '{"path":"qa-moved"}' --expect 200 --check path eq qa-moved
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$PREV" --check sync_in_progress eq false --check dirty_count eq 0 --check path eq qa-moved
  git -C "$B" cat-file -e main:qa-moved/qa-dup/automation.yaml
  git -C "$B" cat-file -e main:qa-moved/qa-dup/tarball/run.sh
  git -C "$B" cat-file -e main:automations/qa-dup/automation.yaml
  control-automation exec --save F13.path-change/tree --expect-output qa-moved/qa-external/automation.yaml --expect-output automations/qa-external/automation.yaml -- git -C "$B" ls-tree -r --name-only main
  control-automation api PUT /v1/git-sync/config --json '{"path":null}' --expect 200 --check path eq automations
  ```
  Both copies are in the tree: the new path got everything, the old
  directories were left for a human to delete.
- **Errors recorded and cleared (`F13.error-recorded`).** Point at a repository that does not exist, then restore.
  Requires: `F13.sync-first`
  ```sh
  LAST_OK=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"$AUTOMATION_VERIFY_RUN/fixtures/qa-missing.git\"}" --expect 200
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_error contains 'git command failed (128): git fetch --prune origin' --check last_error_at exists --check sync_in_progress eq false --check last_synced_at eq "$LAST_OK" --save F13.error-recorded/failed
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"$URL\"}" --expect 200
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at ne "$LAST_OK" --check last_error eq null --check last_error_at eq null --check sync_in_progress eq false --save F13.error-recorded/cleared
  ```
  The trigger itself never fails: the error only shows in status. A failed
  cycle leaves `last_synced_at` where the last success put it.
- **Lease while a cycle runs (`F13.lease`).** A local TCP listener that accepts and never answers holds the cycle in `git fetch` until the 8 s git timeout.
  Requires: `F13.sync-first`
  ```sh
  control-automation helper start --name qa-hang -- python3 -c 'import socket, sys; s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(16); open(sys.argv[1], "w").write(str(s.getsockname()[1])); held = [s.accept() for _ in iter(int, 1)]' "$AUTOMATION_VERIFY_RUN/fixtures/qa-hang.port"
  control-automation exec --timeout 10 -- sh -c 'until test -s "$AUTOMATION_VERIFY_RUN/fixtures/qa-hang.port"; do sleep 0.2; done'
  HANG_PORT=$(cat "$AUTOMATION_VERIFY_RUN/fixtures/qa-hang.port")
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"http://127.0.0.1:$HANG_PORT/qa.git\"}" --expect 200
  T0=$SECONDS
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 5 --check sync_in_progress eq true --check sync_started_at exists --save F13.lease/in-progress
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq false --save F13.lease/second-trigger
  control-automation api GET /v1/git-sync/status --until-ok 30 --check sync_in_progress eq false --check sync_started_at eq null --check last_error contains 'git command timed out after 8.0s: git fetch --prune origin'
  test $((SECONDS - T0)) -ge 7 && test $((SECONDS - T0)) -le 30
  control-automation helper stop qa-hang
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"$URL\"}" --expect 200
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_error eq null --check sync_in_progress eq false
  ```
- **Automatic sync on an interval (`F13.interval`).** No manual trigger: the 15 s loop runs the cycle once the interval has elapsed.
  Requires: `F13.sync-first`
  ```sh
  PREV=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  control-automation api PUT /v1/git-sync/config --json '{"interval_seconds":5}' --expect 200 --check interval_seconds eq 5
  control-automation api GET /v1/git-sync/status --until-ok 45 --check last_synced_at ne "$PREV" --check last_error eq null --check interval_seconds eq 5 --save F13.interval/auto-synced
  control-automation api PUT /v1/git-sync/config --json '{"interval_seconds":0}' --expect 200 --check interval_seconds eq 0
  control-automation api GET /v1/git-sync/status --until-ok 30 --check sync_in_progress eq false
  STOPPED=$(control-automation api GET /v1/git-sync/status --field last_synced_at)
  sleep 20
  control-automation api GET /v1/git-sync/status --check last_synced_at eq "$STOPPED" --check interval_seconds eq 0
  control-automation api PUT /v1/git-sync/config --json '{"interval_seconds":null}' --expect 200 --check interval_seconds eq 0
  ```
  The `sleep 20` is deliberate: the loop's own 15 s tick is what is under
  test, and after it no cycle ran with the interval back at 0.

## Gotchas

- `POST /sync` is fire-and-forget (202). The cycle's outcome appears only in
  `GET /status`: poll `last_synced_at` (changed) or `last_error`, together
  with `sync_in_progress eq false`. A first-ever sync has `last_synced_at`
  null, so poll it with `exists`; later ones compare against the previous
  value. `--field last_synced_at` exits 1 while it is null.
- A failed cycle keeps the previous `last_synced_at`; only `last_error` and
  `last_error_at` move. Both clear on the next success.
- The workdir is `$AUTOMATION_WORKSPACE_BASE/git-sync` (the run's
  `workspaces/git-sync`) unless `AUTOMATION_GIT_SYNC_LOCAL_WORKDIR` is set.
  In local mode there is no per-org subdirectory, and the same checkout is
  reused with `git remote set-url` when the repo URL changes; that is why the
  recipes can switch to a missing or hung remote and back.
- The import is diff-scoped: only directories changed between
  `last_synced_commit` and the new HEAD are read, except on the first cycle
  or when the diff fails. A directory changed by hand in the workdir, or
  committed before the last sync, is not re-imported.
- New automations from git run as the user who last saved the config
  (`configured_by_user_id`); in local mode that is always the local user.
  Anything pushed to the repo becomes a runnable automation, so repo write
  access is admin access.
- An imported directory needs `tarball/` contents or a valid external
  `tarball_source.url`; a skipped directory shows only in the service log
  (`logs --grep 'Skipping invalid automation directory'`), never in
  `last_error`.
- The slug is assigned when the state row is created and never changes, even
  after a rename.
- `GET /status` shows `repo_url` verbatim, including any credentials embedded
  in it; only `/check` details and `last_error` are redacted.
- The 503 `Cannot store the git sync secrets securely` on `PUT /config` is
  unreachable here: `launch` sets `AUTOMATION_KV_SECRET`, which wraps the
  secrets. Local mode without any secret falls back to a 0600 key file at
  `$AUTOMATION_WORKSPACE_BASE/.git-sync-secret-key`.
- The 409 repo-conflict path needs a second org, which local mode cannot
  create through the API; `F13.repo-conflict` seeds one row directly in the
  run's SQLite file and removes it afterwards. That seeded row is also read by
  the background loop (it is skipped, interval 0).
- `ls-remote` in `/check` passes `--` before the URL, but `git clone` (first
  cycle) and `git remote set-url` do not, so a `repo_url` starting with `-`
  could be read as an option there. Not exercised here; a candidate for a
  negative test in the product's own suite.
- `/check` proves read access only and never exercises the encryption key.
  Its timeout is `min(git timeout, 20 s)`.
- The lease TTL is `max(300 s, 5 × git timeout)`: a crashed cycle blocks
  `POST /sync` (`triggered:false`) for up to five minutes.
- The background loop ticks every 15 s regardless of `launch`'s shrunk
  intervals; an interval-driven sync lands within roughly interval + 15 s.

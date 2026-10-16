# Prompt and plugin presets

The no-code way to create an automation: the client sends a prompt (and,
for the plugin preset, the plugins or A/B variants to load) plus a trigger,
and the service builds the tarball itself. It packs the SDK boilerplate
(`main.py`, `finish_tool_hook.py`, `setup.sh`) with the prompt in
`prompt.txt` and optional `repos_config.json`, `plugins_config.json` or
`experiment_config.json`, stores it as an internal upload, and creates an
automation with `setup_script_path: setup.sh` and
`entrypoint: .venv/bin/python main.py`. Editing the prompt later with
`PATCH /v1/{id}` rebuilds the tarball under a new upload. This family proves
what is stored and generated; F07 runs a prompt preset end to end.

Source: `openhands/automation/preset_router.py`, `openhands/automation/presets/`, `openhands/automation/router.py`, `openhands/automation/utils/templates.py`

Routes: `POST /api/automation/v1/preset/prompt`, `POST /api/automation/v1/preset/plugin`, `POST /api/automation/v1/plugin-spec`

## Sub-features

- `F06.prompt-create`: a minimal prompt preset answers 201 with `preset_metadata {preset_type: prompt, prompt}`, `prompt`, `setup_script_path: setup.sh`, `entrypoint: .venv/bin/python main.py`, `timeout: 600`, an `oh-internal://uploads/<id>` tarball path, and a `COMPLETED` upload named `prompt-automation-<name>`.
- `F06.prompt-tarball`: the generated tarball (a gzip archive served as `application/x-tar`) holds exactly `finish_tool_hook.py`, `main.py`, `prompt.txt` and `setup.sh`; `prompt.txt` is the prompt verbatim and `setup.sh` is mode 0755.
- `F06.repos`: `repos` (a list, a single object or a string) is normalized, stored in `preset_metadata.repos` and written to `repos_config.json`; `http://` URLs become `https://`; a short `owner/repo` needs `provider`; other schemes answer 422.
- `F06.prompt-validation`: an empty or missing `prompt`, a missing `trigger`, `agent_profile_id` (presets have no profile support), an unknown field and `state: DRAFT` answer 422; `state: INACTIVE` creates an inactive automation.
- `F06.template-shared`: a preset create whose `template.id` an earlier raw create used answers 200 with that raw automation (one lookup across endpoints) and generates no tarball.
- `F06.patch-prompt`: PATCH `prompt` on a preset writes a new upload whose `prompt.txt` is the new prompt and whose other members are byte-identical, updates `preset_metadata.prompt`, and soft-deletes the old upload; an unchanged prompt keeps the tarball.
- `F06.plugin-create`: a plugin preset answers 201 with `preset_metadata {preset_type: plugin, prompt, plugins}` and a tarball that adds `plugins_config.json`; a single plugin object is normalized to a list.
- `F06.plugin-validation`: an empty `plugins` list, both or neither of `plugins`/`variants`, variants without `experiment_id`, one variant, duplicate variant names, `experiment_id` with `plugins`, a zero weight and an absolute or `..` `repo_path` each answer 422 with their own message.
- `F06.plugin-variants`: valid A/B variants produce `experiment_config.json` (each variant's model falling back to the automation `model`) and no `plugins_config.json`; `preset_metadata` has no `plugins` key.
- `F06.plugin-spec-create`: a plugin-spec create reads an executable plugin manifest, resolves defaults from its config schema, stores `preset_metadata.preset_type: plugin_spec`, stores `plugin_spec.plugin` and `plugin_spec.config`, uses `tarball_path: plugin-spec://runtime`, and rejects custom event sources that have no webhook configured.
- `F06.continue-keep-alive`, known bug: a preset with an event trigger whose `destination` is `continue_conversation` should store `keep_alive: true` as `POST /v1` does; it stores `null`.
- `F06.continue-keep-alive-false`, known bug: a preset with `continue_conversation` and `keep_alive: false` should answer 422 as `POST /v1` does; it answers 201.
- `F06.continue-patch-enforced`: any later PATCH of such a preset forces `keep_alive: true`, and PATCH `keep_alive: false` answers 400.
- `F06.auth`: both preset routes answer 401 without a key and create nothing.

## How to get to it (agent POV)

- REST: `POST /v1/preset/prompt` (`CreatePromptAutomationRequest`),
  `POST /v1/preset/plugin` (`CreatePluginAutomationRequest`), and
  `POST /v1/plugin-spec` (`CreatePluginSpecAutomationRequest`), all
  `extra="forbid"`; responses are the F05 `AutomationResponse`. Reading,
  editing, deleting and downloading a preset go through the F05 routes
  (`GET/PATCH/DELETE /v1/{id}`, `GET /v1/{id}/tarball`), which these recipes
  use as the second view.
- CLI: `control-automation api POST /v1/preset/prompt --json '{...}'`; the
  tarball is checked with `api GET /v1/$ID/tarball --raw-out FILE` and
  `tar tzf FILE` / `tar xzOf FILE prompt.txt`.
- Agent Canvas's "new automation from a prompt" form and the `automation`
  skill in `OpenHands/extensions` (one-click templates with `template`
  provenance) call these routes.
- At run time the generated `setup.sh` installs `openhands-sdk` at the
  version `/sdk-version` reports (F01) and `main.py` reads `prompt.txt`,
  `repos_config.json`, `plugins_config.json` or `experiment_config.json`
  (F07 drives a real run).
- Config: `FILE_STORE` (the generated tarball is stored like any upload);
  `AUTOMATION_DEFAULT_RUN_DURATION` / `AUTOMATION_MAX_RUN_DURATION`; local
  mode stores `model` as given (or `null`) instead of resolving a profile.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed: no
  preset here is dispatched (yearly cron, or an event source nothing sends).
- Every bullet downloads tarballs into `$AUTOMATION_VERIFY_RUN` and reads
  them with the shell's `tar`.

- **Create a prompt preset (`F06.prompt-create`).** The automation and its generated upload.
  ```sh
  PAID=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa-prompt","prompt":"Create a file named hello.txt containing exactly: hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --check preset_metadata.preset_type eq prompt --check preset_metadata.prompt eq 'Create a file named hello.txt containing exactly: hi' --check prompt eq 'Create a file named hello.txt containing exactly: hi' --check setup_script_path eq setup.sh --check entrypoint eq '.venv/bin/python main.py' --check timeout eq 600 --check state eq ACTIVE --check agent_profile_id eq null --check tarball_path matches '^oh-internal://uploads/[0-9a-f-]{36}$' --save F06.prompt-create/create --field id)
  PTP=$(control-automation api GET "/v1/$PAID" --expect 200 --check preset_metadata.preset_type eq prompt --field tarball_path)
  control-automation api GET "/v1/uploads/${PTP#oh-internal://uploads/}" --expect 200 --check name eq prompt-automation-qa-prompt --check status eq COMPLETED --check description contains 'Auto-generated from prompt: Create a file' --save F06.prompt-create/upload
  ```
- **Generated prompt tarball (`F06.prompt-tarball`).** Four members, the prompt verbatim, an executable setup script.
  ```sh
  TBID=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa-prompt-tar","prompt":"Line one\nLine two with \"quotes\" and ünïcode","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --field id)
  control-automation api GET "/v1/$TBID/tarball" --expect 200 --quiet --check-header content-type eq application/x-tar --check-header content-disposition eq 'attachment; filename="qa-prompt-tar.tar"' --raw-out "$AUTOMATION_VERIFY_RUN/qa-prompt.tar" --save F06.prompt-tarball/download
  test "$(tar tzf "$AUTOMATION_VERIFY_RUN/qa-prompt.tar" | sort | paste -sd, -)" = finish_tool_hook.py,main.py,prompt.txt,setup.sh
  test "$(tar xzOf "$AUTOMATION_VERIFY_RUN/qa-prompt.tar" prompt.txt)" = "$(printf 'Line one\nLine two with "quotes" and ünïcode')"
  tar tvzf "$AUTOMATION_VERIFY_RUN/qa-prompt.tar" setup.sh | grep -q '^-rwxr-xr-x'
  tar xzOf "$AUTOMATION_VERIFY_RUN/qa-prompt.tar" setup.sh | grep -q 'sdk-version'
  tar xzOf "$AUTOMATION_VERIFY_RUN/qa-prompt.tar" main.py | grep -q 'prompt.txt'
  ```
  `tar tzf` (gzip) is required although the response says
  `application/x-tar`. `setup.sh` fetches `/sdk-version` to pick the SDK to
  install; `main.py` reads `prompt.txt`.
- **Repositories (`F06.repos`).** Normalized, stored and written to `repos_config.json`.
  ```sh
  RPID=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa-repos","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"repos":[{"url":"http://github.com/qa-org/qa-repo","ref":"main"},{"url":"qa-org/other","provider":"gitlab"}]}' --expect 201 --check preset_metadata.repos.0.url eq https://github.com/qa-org/qa-repo --check preset_metadata.repos.0.ref eq main --check preset_metadata.repos.1.provider eq gitlab --save F06.repos/list --field id)
  control-automation api GET "/v1/$RPID/tarball" --expect 200 --quiet --raw-out "$AUTOMATION_VERIFY_RUN/qa-repos.tar"
  tar tzf "$AUTOMATION_VERIFY_RUN/qa-repos.tar" | grep -qx repos_config.json
  tar xzOf "$AUTOMATION_VERIFY_RUN/qa-repos.tar" repos_config.json | grep -q '"url": "https://github.com/qa-org/qa-repo"'
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-repos-one","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"repos":"git@github.com:qa-org/qa-repo.git"}' --expect 201 --check preset_metadata.repos len-eq 1 --check preset_metadata.repos.0.url eq git@github.com:qa-org/qa-repo.git
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-repos-short","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"repos":"qa-org/qa-repo"}' --expect 422 --check detail.0.msg contains "requires explicit 'provider' field" --save F06.repos/short-no-provider
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-repos-ftp","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"repos":"ftp://qa.invalid/r"}' --expect 422 --check detail.0.loc contains url
  ```
  A preset without `repos` has no `repos_config.json` (`F06.prompt-tarball`).
- **Prompt preset validation (`F06.prompt-validation`).** Each refused body creates nothing.
  ```sh
  N0=$(control-automation state sql "SELECT count(*) AS n FROM automations" --field rows.0.n)
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv","prompt":"","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 422 --check detail.0.loc.1 eq prompt --check detail.0.type eq string_too_short --save F06.prompt-validation/empty-prompt
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 422 --check detail.0.type eq missing --check detail.0.loc.1 eq prompt
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv","prompt":"hi"}' --expect 422 --check detail.0.type eq missing --check detail.0.loc.1 eq trigger
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"agent_profile_id":"11111111-1111-4111-8111-111111111111"}' --expect 422 --check detail.0.type eq extra_forbidden --check detail.0.loc.1 eq agent_profile_id
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"tarball_path":"https://qa.invalid/x.tgz"}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"state":"DRAFT"}' --expect 422 --check detail.0.msg contains 'state=DRAFT is reserved'
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"timeout":1801}' --expect 422 --check detail.0.msg contains 'must not exceed 1800 seconds'
  control-automation state sql "SELECT count(*) AS n FROM automations" --check rows.0.n eq "$N0"
  control-automation api POST /v1/preset/prompt --json '{"name":"qa-pv-inactive","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"state":"INACTIVE","timeout":120,"model":"qa-model"}' --expect 201 --check state eq INACTIVE --check enabled eq false --check timeout eq 120 --check model eq qa-model
  ```
- **Template lookup shared with raw create (`F06.template-shared`).** A preset repeat of a raw template returns the raw automation.
  ```sh
  STPL="qa-shared-tpl-$(date +%s%N)"
  SRID=$(control-automation api POST /v1 --json "{\"name\":\"qa-shared-raw\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"https://qa.invalid/x.tgz\",\"entrypoint\":\"bash run.sh\",\"template\":{\"id\":\"$STPL\",\"version\":\"1\"}}" --expect 201 --field id)
  U0=$(control-automation state sql "SELECT count(*) AS n FROM tarball_uploads" --field rows.0.n)
  control-automation api POST /v1/preset/prompt --json "{\"name\":\"qa-shared-prompt\",\"prompt\":\"hi\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"template\":{\"id\":\"$STPL\",\"version\":\"2\"}}" --expect 200 --check id eq "$SRID" --check name eq qa-shared-raw --check preset_metadata.preset_type missing --check tarball_path eq https://qa.invalid/x.tgz --save F06.template-shared/prompt-repeat
  control-automation api POST /v1/preset/plugin --json "{\"name\":\"qa-shared-plugin\",\"prompt\":\"hi\",\"plugins\":[{\"source\":\"github:qa-org/qa-plugin\"}],\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"template\":{\"id\":\"$STPL\",\"version\":\"3\"}}" --expect 200 --check id eq "$SRID"
  control-automation state sql "SELECT count(*) AS n FROM tarball_uploads" --check rows.0.n eq "$U0"
  ```
  The lookup runs first, so no tarball is generated for a repeat.
- **PATCH the prompt (`F06.patch-prompt`).** New upload, new `prompt.txt`, everything else identical, old upload retired.
  ```sh
  EPID=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa-edit","prompt":"old prompt","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"repos":[{"url":"https://github.com/qa-org/qa-repo"}]}' --expect 201 --field id)
  OLDTP=$(control-automation api GET "/v1/$EPID" --expect 200 --field tarball_path)
  control-automation api GET "/v1/$EPID/tarball" --expect 200 --quiet --raw-out "$AUTOMATION_VERIFY_RUN/qa-edit-old.tar"
  control-automation api PATCH "/v1/$EPID" --json '{"prompt":"new prompt"}' --expect 200 --check prompt eq 'new prompt' --check preset_metadata.prompt eq 'new prompt' --check tarball_path ne "$OLDTP" --check tarball_path matches '^oh-internal://uploads/' --save F06.patch-prompt/patch
  NEWTP=$(control-automation api GET "/v1/$EPID" --expect 200 --check preset_metadata.repos.0.url eq https://github.com/qa-org/qa-repo --field tarball_path)
  control-automation api GET "/v1/$EPID/tarball" --expect 200 --quiet --raw-out "$AUTOMATION_VERIFY_RUN/qa-edit-new.tar" --save F06.patch-prompt/download
  test "$(tar xzOf "$AUTOMATION_VERIFY_RUN/qa-edit-new.tar" prompt.txt)" = 'new prompt'
  for m in main.py finish_tool_hook.py setup.sh repos_config.json; do test "$(tar xzOf "$AUTOMATION_VERIFY_RUN/qa-edit-old.tar" "$m" | sha256sum)" = "$(tar xzOf "$AUTOMATION_VERIFY_RUN/qa-edit-new.tar" "$m" | sha256sum)"; done
  control-automation api GET "/v1/uploads/${OLDTP#oh-internal://uploads/}" --expect 404 --check detail eq 'Upload not found'
  control-automation api GET "/v1/uploads/${NEWTP#oh-internal://uploads/}" --expect 200 --check name eq prompt-automation-qa-edit-edit --check status eq COMPLETED
  control-automation api PATCH "/v1/$EPID" --json '{"prompt":"new prompt"}' --expect 200 --check tarball_path eq "$NEWTP"
  control-automation api PATCH "/v1/$EPID" --json '{"name":"qa-edit-renamed"}' --expect 200 --check tarball_path eq "$NEWTP"
  ```
  The old upload is soft-deleted in the same transaction and its stored
  object removed after the commit. Only a prompt change regenerates.
- **Create a plugin preset (`F06.plugin-create`).** Plugins are stored and written to `plugins_config.json`.
  ```sh
  PLID=$(control-automation api POST /v1/preset/plugin --json '{"name":"qa-plugin","prompt":"/qa-plugin:hello","plugins":[{"source":"github:qa-org/qa-plugin","ref":"v1","repo_path":"plugins/qa"}],"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --check preset_metadata.preset_type eq plugin --check preset_metadata.prompt eq /qa-plugin:hello --check preset_metadata.plugins.0.source eq github:qa-org/qa-plugin --check preset_metadata.plugins.0.repo_path eq plugins/qa --check entrypoint eq '.venv/bin/python main.py' --check setup_script_path eq setup.sh --save F06.plugin-create/create --field id)
  PLTP=$(control-automation api GET "/v1/$PLID" --expect 200 --field tarball_path)
  control-automation api GET "/v1/uploads/${PLTP#oh-internal://uploads/}" --expect 200 --check name eq plugin-automation-qa-plugin
  control-automation api GET "/v1/$PLID/tarball" --expect 200 --quiet --raw-out "$AUTOMATION_VERIFY_RUN/qa-plugin.tar" --save F06.plugin-create/download
  test "$(tar tzf "$AUTOMATION_VERIFY_RUN/qa-plugin.tar" | sort | paste -sd, -)" = finish_tool_hook.py,main.py,plugins_config.json,prompt.txt,setup.sh
  tar xzOf "$AUTOMATION_VERIFY_RUN/qa-plugin.tar" plugins_config.json | grep -q '"ref": "v1"'
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plugin-one","prompt":"hi","plugins":{"source":"github:qa-org/qa-plugin"},"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --check preset_metadata.plugins len-eq 1 --check preset_metadata.plugins.0.source eq github:qa-org/qa-plugin
  ```
- **Create a plugin-spec automation (`F06.plugin-spec-create`).** The executable manifest path preflights its source before storing an automation.
  ```sh
  control-automation api POST /v1/plugin-spec --json '{"name":"qa-plugin-spec","plugin":{"source":"github:qa-org/qa-plugin","ref":"main"},"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 422 --check detail contains 'Failed to preflight automation plugin' --save F06.plugin-spec-create/missing-plugin
  ```
  A real catalog-backed plugin source answers 201 with `preset_metadata.preset_type: plugin_spec`, `tarball_path: plugin-spec://runtime`, `setup_script_path: setup.sh`, and the manifest-derived entrypoint; this negative recipe drives the route without depending on an external test repository.

  Plugins are not fetched at create time; the run's `main.py` loads them.
- **Plugin preset validation (`F06.plugin-validation`).** One message per rule.
  ```sh
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"plugins":[]}' --expect 422 --check detail.0.msg eq 'Value error, At least one plugin is required' --save F06.plugin-validation/empty
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 422 --check detail.0.msg eq "Value error, Exactly one of 'plugins' or 'variants' must be provided."
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"plugins":[{"source":"github:o/p"}],"variants":[{"name":"a","weight":1,"plugins":[{"source":"github:o/p"}]},{"name":"b","weight":1,"plugins":[{"source":"github:o/q"}]}],"experiment_id":"qa-exp"}' --expect 422 --check detail.0.msg eq "Value error, Exactly one of 'plugins' or 'variants' must be provided."
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"variants":[{"name":"a","weight":1,"plugins":[{"source":"github:o/p"}]},{"name":"b","weight":1,"plugins":[{"source":"github:o/q"}]}]}' --expect 422 --check detail.0.msg eq "Value error, 'experiment_id' is required when using 'variants'."
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"variants":[{"name":"a","weight":1,"plugins":[{"source":"github:o/p"}]}],"experiment_id":"qa-exp"}' --expect 422 --check detail.0.msg eq 'Value error, At least two variants are required for an A/B test.'
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"variants":[{"name":"a","weight":1,"plugins":[{"source":"github:o/p"}]},{"name":"a","weight":1,"plugins":[{"source":"github:o/q"}]}],"experiment_id":"qa-exp"}' --expect 422 --check detail.0.msg eq 'Value error, Variant names must be unique.'
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"plugins":[{"source":"github:o/p"}],"experiment_id":"qa-exp"}' --expect 422 --check detail.0.msg eq "Value error, 'experiment_id' can only be used with 'variants', not 'plugins'."
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"variants":[{"name":"a","weight":0,"plugins":[{"source":"github:o/p"}]},{"name":"b","weight":1,"plugins":[{"source":"github:o/q"}]}],"experiment_id":"qa-exp"}' --expect 422 --check detail.0.type eq greater_than --check detail.0.loc contains weight
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"plugins":[{"source":"github:o/p","repo_path":"/abs"}]}' --expect 422 --check detail.0.msg eq 'Value error, repo_path must be relative, not absolute'
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-plv","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"},"plugins":[{"source":"github:o/p","repo_path":"a/../b"}]}' --expect 422 --check detail.0.msg eq "Value error, repo_path cannot contain '..' (parent directory traversal)"
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name = 'qa-plv'" --check rows.0.n eq 0
  ```
  More than 10 variants answers `At most 10 variants are allowed.` by the
  same path.
- **A/B variants (`F06.plugin-variants`).** `experiment_config.json` replaces `plugins_config.json`.
  ```sh
  VRID=$(control-automation api POST /v1/preset/plugin --json '{"name":"qa-variants","prompt":"hi","model":"qa-default-model","experiment_id":"qa-exp","variants":[{"name":"control","weight":3,"plugins":[{"source":"github:qa-org/a"}]},{"name":"treatment","weight":1,"model":"qa-other-model","plugins":[{"source":"github:qa-org/b","ref":"v2"}]}],"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --check preset_metadata.preset_type eq plugin --check preset_metadata.plugins missing --check model eq qa-default-model --save F06.plugin-variants/create --field id)
  control-automation api GET "/v1/$VRID/tarball" --expect 200 --quiet --raw-out "$AUTOMATION_VERIFY_RUN/qa-variants.tar"
  test "$(tar tzf "$AUTOMATION_VERIFY_RUN/qa-variants.tar" | sort | paste -sd, -)" = experiment_config.json,finish_tool_hook.py,main.py,prompt.txt,setup.sh
  tar xzOf "$AUTOMATION_VERIFY_RUN/qa-variants.tar" experiment_config.json > "$AUTOMATION_VERIFY_RUN/qa-experiment.json"
  control-automation state cat qa-experiment.json --check experiment_id eq qa-exp --check variants.0.name eq control --check variants.0.weight eq 3 --check variants.0.model eq qa-default-model --check variants.1.model eq qa-other-model --check variants.1.plugins.0.ref eq v2
  ```
  A variant without `model` inherits the automation's `model`; in local
  mode a missing automation `model` leaves the variant's `null`.
- **Preset continue_conversation keep_alive (`F06.continue-keep-alive`), known bug.** The preset should force `keep_alive: true` like `POST /v1` does.
  ```sh
  CKID=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa-preset-continue","prompt":"hi","trigger":{"type":"event","source":"qa-src","on":"qa.*","destination":"continue_conversation","subject_key_expr":"id"}}' --expect 201 --field id)
  control-automation api GET "/v1/$CKID" --expect 200 --check trigger.destination eq continue_conversation --check keep_alive eq true --save F06.continue-keep-alive/get  # bug
  ```
  Today `keep_alive` is stored as `null`. The watchdog and the run-complete
  handler clean up a sandbox unless `keep_alive` is `true`, trusting that
  `continue_conversation` automations were forced at creation, so the
  sandbox (and the conversation the next event should continue) is deleted
  after each run. Raw creates do it right (`F05.keep-alive-continue`).
- **Preset continue_conversation with keep_alive false (`F06.continue-keep-alive-false`), known bug.** The preset should refuse it like `POST /v1` does.
  ```sh
  control-automation api POST /v1/preset/plugin --json '{"name":"qa-preset-continue-off","prompt":"hi","plugins":[{"source":"github:qa-org/qa-plugin"}],"keep_alive":false,"trigger":{"type":"event","source":"qa-src","on":"qa.*","destination":"continue_conversation","subject_key_expr":"id"}}' --expect 422 --save F06.continue-keep-alive-false/create  # bug
  ```
  Today it answers 201 with `keep_alive: false`. The prompt preset behaves
  the same.
- **A later PATCH enforces keep_alive (`F06.continue-patch-enforced`).** The PATCH handler applies the rule the preset create skipped.
  ```sh
  CPID=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa-preset-continue-patch","prompt":"hi","trigger":{"type":"event","source":"qa-src","on":"qa.*","destination":"continue_conversation","subject_key_expr":"id"}}' --expect 201 --field id)
  control-automation api PATCH "/v1/$CPID" --json '{"name":"qa-preset-continue-patched"}' --expect 200 --check keep_alive eq true --save F06.continue-patch-enforced/rename
  control-automation api GET "/v1/$CPID" --expect 200 --check keep_alive eq true
  control-automation api PATCH "/v1/$CPID" --json '{"keep_alive":false}' --expect 400 --check detail contains "keep_alive cannot be false when destination is 'continue_conversation'"
  ```
  Any PATCH, even a rename, settles `keep_alive: true`; until then the
  preset runs with cleanup enabled.
- **Key required (`F06.auth`).** No key, no preset, no upload.
  ```sh
  U0=$(control-automation state sql "SELECT count(*) AS n FROM tarball_uploads" --field rows.0.n)
  control-automation api POST /v1/preset/prompt --auth none --json '{"name":"qa-noauth","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 401 --save F06.auth/prompt
  control-automation api POST /v1/preset/plugin --auth bad --json '{"name":"qa-noauth","prompt":"hi","plugins":[{"source":"github:o/p"}],"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 401
  control-automation state sql "SELECT count(*) AS n FROM tarball_uploads" --check rows.0.n eq "$U0"
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name = 'qa-noauth'" --check rows.0.n eq 0
  ```

## Gotchas

- Preset tarballs are gzip (`w:gz`) although the download says
  `application/x-tar` and names the file `.tar`; list them with `tar tzf`.
- Presets have no `agent_profile_id`, `tarball_path`, `entrypoint` or
  `setup_script_path` fields; sending any is a 422 `extra_forbidden`.
- The template lookup is shared with `POST /v1` and runs before model
  resolution and tarball generation: a repeat returns whatever automation
  first used that `template.id` in the org, raw or preset, with HTTP 200.
- Prompt edits leave a trail of uploads: the original is soft-deleted and
  a `prompt-automation-<name>-edit` upload replaces it. PATCH regenerates
  for any automation whose internal tarball contains `prompt.txt`,
  including a raw one that happens to ship such a file.
- `continue_conversation` is not enforced at preset creation
  (`F06.continue-keep-alive`, `F06.continue-keep-alive-false`); send
  `keep_alive: true` explicitly, or PATCH once after creating.
- `model` is stored as given in local mode (no profile list); in cloud mode
  an unknown name is 422 `Model profile ... not found`, and an omitted one
  takes the user's active profile at creation time.
- Preset source files (`presets/prompt/sdk_main.py`, `setup.sh`,
  `finish_tool_hook.py`) are cached in process memory; a change to them
  needs a service restart before new presets pick it up.
- Real runs of presets need an LLM profile on the Agent Server and PyPI
  access for `setup.sh`; they are covered in F07, not here.

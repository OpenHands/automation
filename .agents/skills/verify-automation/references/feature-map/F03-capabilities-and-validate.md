# Capabilities and preflight validation

What a setup UI asks before it creates anything. `GET /v1/capabilities`
describes what this deployment (and the caller's organization) supports:
whether it can accept work, the trigger kinds and event sources it can
deliver, the cron floor and timezones, the filter functions, feature
flags such as `customTarball` and `kvStore`, and the Git providers whose
repositories a run clones. `POST /v1/validate` checks a
draft creation body against the creation endpoint it is meant for and
answers 200 with field-addressed errors, plus whether a sample event would
fire an event trigger. Neither endpoint writes. Both speak camelCase, unlike
the rest of the API.

Source: `openhands/automation/capabilities_router.py`, `openhands/automation/schemas.py`, `openhands/automation/draft_schemas.py`, `openhands/automation/providers.py`, `openhands/automation/utils/cron.py`, `openhands/automation/utils/model_profiles.py`, `openhands/automation/utils/webhook.py`

Routes: `GET /api/automation/v1/capabilities`, `POST /api/automation/v1/validate`

## Sub-features

- `F03.caps-default`: on a default local run capabilities answer `ready:true`, `maxAutomationTimeoutSeconds:1800`, `triggerKinds:["cron"]`, no event sources or event types, no `triggers.event`, the sorted feature list (with `customTarball`, `agentProfiles` and `kvStore`), and `repoProviders` sorted (`bitbucket`, `github`, `gitlab` with the pinned SDK).
- `F03.caps-cron`: `triggers.cron.minIntervalSeconds` is `max(60, scheduler interval)` (60 on a run), and `timezones` lists every IANA zone including `UTC`.
- `F03.auth`: both routes need a key (401 without one).
- `F03.caps-custom-webhook`: an enabled custom webhook of the caller's org adds its source, the `event` trigger kind, `webhookDelivery` and `triggers.event` (`filterLanguage:"jmespath"`, `filterFunctions`); `eventTypes` stays empty.
- `F03.caps-webhook-disabled`: disabling that webhook removes the source and everything it added.
- `F03.caps-builtin-sources`: with `AUTOMATION_WEBHOOK_SECRET` set, `bitbucket_data_center`, `github` and `jira_dc` join the sources and `eventTypes` lists the GitHub patterns.
- `F03.caps-config`: `AUTOMATION_SCHEDULER_INTERVAL_SECONDS`, `AUTOMATION_MAX_RUN_DURATION` and `AUTOMATION_KV_SECRET` move `minIntervalSeconds`, `maxAutomationTimeoutSeconds` and the `kvStore` feature.
- `F03.caps-not-ready`: in cloud mode without `AUTOMATION_SERVICE_KEY` capabilities answer `ready:false` with every list empty and `triggers:{}`.
- `F03.validate-valid`: a valid preset-prompt draft and a valid raw `/v1` draft answer 200 `{valid:true, errors:[], sampleEventMatched:null}`; the envelope accepts camelCase or snake_case keys.
- `F03.validate-schema-errors`: an invalid draft answers 200 `valid:false` with Pydantic errors addressed by dotted path (`name` missing, unknown key `extra_forbidden`, `repos[0].url`, `trigger.schedule` invalid cron, `trigger.timezone`, `timeout` over the maximum).
- `F03.validate-envelope`: a bad `endpoint` or an unknown envelope key is a 422, not a 200.
- `F03.validate-interval-too-short`: a schedule faster than the deployment's floor gets `interval_too_short` on `trigger.schedule`.
- `F03.validate-event-source`: an event trigger on a source nothing delivers gets `event_source_not_configured` on `trigger.source`.
- `F03.validate-event-type`: with GitHub built in, a pattern GitHub cannot deliver gets `event_type_not_delivered` on `trigger.on`.
- `F03.validate-sample-event`: for a configured source, `sampleEvent` reports `sampleEventMatched` true or false (event key and filter), and a payload the source cannot parse gets `unparseable_sample_event`.
- `F03.validate-agent-profile`: on `/v1`, `agent_profile_id` with `model` gets `invalid_agent_profile` ("An agent profile already specifies the model"); a non-UUID id is a schema error; in local mode a profile alone is not looked up.
- `F03.validate-advisory`: validate writes nothing and does not run every creation check: an `http://` tarball validates but `POST /v1` refuses it with 422.

## How to get to it (agent POV)

- REST: `GET /api/automation/v1/capabilities`,
  `POST /api/automation/v1/validate` with
  `{"endpoint": "/v1" | "/v1/preset/prompt" | "/v1/preset/plugin", "draft": {...}, "automationId"?: str, "sampleEvent"?: {...}}`.
  The response models are camelCase (`maxAutomationTimeoutSeconds`,
  `sampleEventMatched`); the draft inside is the snake_case creation body.
- Consumers: the setup contract is owned by `OpenHands/extensions` (its
  catalog and TypeScript types are camelCase); Agent Canvas and the
  `automation` skill read capabilities to decide which options to offer and
  call validate before `POST /v1`, `/v1/preset/prompt` or
  `/v1/preset/plugin`.
- Config: `AUTOMATION_WEBHOOK_SECRET` (built-in sources),
  `AUTOMATION_KV_SECRET` (`kvStore`, launch always sets it),
  `AUTOMATION_SCHEDULER_INTERVAL_SECONDS` (cron floor),
  `AUTOMATION_MAX_RUN_DURATION` (timeout ceiling), `AUTOMATION_SERVICE_KEY`
  and `AUTOMATION_AGENT_SERVER_URL` (readiness in cloud mode). Custom
  sources come from `POST /v1/webhooks` (webhook family).

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM is needed.
- `F03.caps-custom-webhook` creates the custom webhook `qa-caps` that the
  sample-event bullet uses; `F03.caps-webhook-disabled` disables it at the
  end. Bullets that restart the service restore it with `--reset-env`.

- **Default capabilities (`F03.caps-default`).** The full feature list of a local run, which always has a KV secret.
  ```sh
  control-automation api GET /v1/capabilities --expect 200 --quiet --check ready eq true --check maxAutomationTimeoutSeconds eq 1800 --check triggerKinds eq '["cron"]' --check eventSources len-eq 0 --check eventTypes len-eq 0 --check triggers.event missing --check features eq '["agentProfiles", "automationDrafts", "conversationDispatch", "customTarball", "kvStore", "mcpTools", "presetPlugin", "presetPrompt", "repoClone"]' --check repoProviders eq '["bitbucket", "github", "gitlab"]' --check max_automation_timeout_seconds missing --save F03.caps-default/capabilities
  ```
- **Cron constraints (`F03.caps-cron`).** The run's scheduler polls every 5 s, but the floor never goes below 60.
  ```sh
  control-automation api GET /v1/capabilities --expect 200 --quiet --check triggers.cron.minIntervalSeconds eq 60 --check triggers.cron.timezones contains UTC --check triggers.cron.timezones contains Europe/Berlin --check triggers.cron.timezones len-ge 400 --save F03.caps-cron/cron
  ```
- **Key required (`F03.auth`).** Both routes refuse a request without a key.
  ```sh
  control-automation api GET /v1/capabilities --auth none --expect 401 --check detail matches '^Authentication required'
  control-automation api POST /v1/validate --auth none --json '{"endpoint":"/v1","draft":{}}' --expect 401 --check detail matches '^Authentication required' --save F03.auth/validate
  ```
- **Custom webhook adds event delivery (`F03.caps-custom-webhook`).** Create an enabled custom webhook, then read capabilities again.
  ```sh
  CWID=$(control-automation api POST /v1/webhooks --json '{"name":"qa caps hook","source":"qa-caps"}' --expect 201 --field id)
  control-automation api GET /v1/capabilities --expect 200 --quiet --check eventSources eq '["qa-caps"]' --check triggerKinds eq '["cron", "event"]' --check features contains webhookDelivery --check triggers.event.filterLanguage eq jmespath --check triggers.event.filterFunctions contains glob --check triggers.event.filterFunctions contains regex --check eventTypes len-eq 0 --save F03.caps-custom-webhook/capabilities
  ```
- **Sample events against the custom source (`F03.validate-sample-event`).** The default event key expression is `type`; the filter is JMESPath over the payload.
  Requires: `F03.caps-custom-webhook`
  ```sh
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"qa-caps","on":"order.*"}},"sampleEvent":{"type":"order.created"}}' --expect 200 --check valid eq true --check sampleEventMatched eq true --save F03.validate-sample-event/matched
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"qa-caps","on":"order.*"}},"sampleEvent":{"type":"user.created"}}' --expect 200 --check valid eq true --check sampleEventMatched eq false
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"qa-caps","on":"order.*","filter":"amount > `10`"}},"sample_event":{"type":"order.created","amount":50}}' --expect 200 --check sampleEventMatched eq true
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"qa-caps","on":"order.*","filter":"amount > `10`"}},"sample_event":{"type":"order.created","amount":5}}' --expect 200 --check sampleEventMatched eq false
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"qa-caps","on":"order.*"}},"sampleEvent":{"foo":1}}' --expect 200 --check valid eq false --check errors.0.field eq sampleEvent --check errors.0.code eq unparseable_sample_event --check sampleEventMatched eq null --save F03.validate-sample-event/unparseable
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"qa-caps","on":"order.*"}}}' --expect 200 --check valid eq true --check sampleEventMatched eq null
  ```
- **Disabled webhook drops out (`F03.caps-webhook-disabled`).** Disable it and read capabilities again; the webhook itself still exists.
  Requires: `F03.caps-custom-webhook`
  ```sh
  control-automation api PATCH "/v1/webhooks/$CWID" --json '{"enabled":false}' --expect 200 --check enabled eq false --quiet
  control-automation api GET /v1/capabilities --expect 200 --quiet --check eventSources len-eq 0 --check triggerKinds eq '["cron"]' --check triggers.event missing --check features not-contains webhookDelivery --save F03.caps-webhook-disabled/capabilities
  control-automation api GET "/v1/webhooks/$CWID" --expect 200 --check source eq qa-caps
  ```
- **Valid drafts (`F03.validate-valid`).** A preset-prompt draft and a raw draft with an https tarball; the envelope takes either key style.
  ```sh
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 200 --check valid eq true --check errors len-eq 0 --check sampleEventMatched eq null --check sample_event_matched missing --save F03.validate-valid/prompt
  control-automation api POST /v1/validate --json '{"endpoint":"/v1","draft":{"name":"qa","trigger":{"type":"cron","schedule":"0 9 * * 1","timezone":"Europe/Berlin"},"tarball_path":"https://example.com/x.tar.gz","entrypoint":"bash run.sh"},"automationId":"qa-entry"}' --expect 200 --check valid eq true
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"}},"automation_id":"qa-entry","sample_event":null}' --expect 200 --check valid eq true
  ```
- **Schema errors (`F03.validate-schema-errors`).** Each is 200 `valid:false` with the field path, Pydantic code and message.
  ```sh
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 200 --check valid eq false --check errors.0.field eq name --check errors.0.code eq missing --save F03.validate-schema-errors/missing-name
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","bogus":1,"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 200 --check errors.0.field eq bogus --check errors.0.code eq extra_forbidden
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","repos":[{"url":""}],"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 200 --check errors.0.field eq 'repos[0].url' --check errors.0.code eq value_error
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"cron","schedule":"not a cron"}}}' --expect 200 --check errors.0.field eq trigger.schedule --check errors.0.code eq value_error --check errors.0.message contains 'Invalid cron expression: not a cron' --save F03.validate-schema-errors/bad-cron
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *","timezone":"Mars/Olympus"}}}' --expect 200 --check errors.0.field eq trigger.timezone --check errors.0.message contains 'Invalid timezone: Mars/Olympus'
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","timeout":5000,"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 200 --check errors.0.field eq timeout --check errors.0.message contains 'timeout must not exceed 1800 seconds (30 minutes)'
  ```
- **Envelope errors (`F03.validate-envelope`).** Only a malformed request envelope is a 4xx.
  ```sh
  control-automation api POST /v1/validate --json '{"endpoint":"/v2","draft":{}}' --expect 422 --check detail.0.loc contains endpoint --check detail.0.type eq literal_error --save F03.validate-envelope/endpoint
  control-automation api POST /v1/validate --json '{"endpoint":"/v1","draft":{},"extra":1}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api POST /v1/validate --json '{"endpoint":"/v1"}' --expect 422 --check detail.0.loc contains draft
  ```
- **Unconfigured event source (`F03.validate-event-source`).** Without the built-in secret nothing delivers `github` events.
  ```sh
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"github","on":"push"}}}' --expect 200 --check valid eq false --check errors.0.field eq trigger.source --check errors.0.code eq event_source_not_configured --check errors.0.message eq "No webhook is configured to deliver 'github' events to this deployment." --save F03.validate-event-source/github
  ```
- **Agent profile rules (`F03.validate-agent-profile`).** A profile already carries the model; local mode does not look the profile up.
  ```sh
  control-automation api POST /v1/validate --json '{"endpoint":"/v1","draft":{"name":"qa","model":"m","agent_profile_id":"6f1c2a1e-0000-4000-8000-000000000001","trigger":{"type":"cron","schedule":"0 9 * * 1"},"tarball_path":"https://example.com/x.tar.gz","entrypoint":"bash run.sh"}}' --expect 200 --check valid eq false --check errors.0.field eq agent_profile_id --check errors.0.code eq invalid_agent_profile --check errors.0.message eq 'An agent profile already specifies the model' --save F03.validate-agent-profile/with-model
  control-automation api POST /v1/validate --json '{"endpoint":"/v1","draft":{"name":"qa","agent_profile_id":"not-a-uuid","trigger":{"type":"cron","schedule":"0 9 * * 1"},"tarball_path":"https://example.com/x.tar.gz","entrypoint":"bash run.sh"}}' --expect 200 --check errors.0.field eq agent_profile_id --check errors.0.code eq uuid_parsing
  control-automation api POST /v1/validate --json '{"endpoint":"/v1","draft":{"name":"qa","agent_profile_id":"6f1c2a1e-0000-4000-8000-000000000001","trigger":{"type":"cron","schedule":"0 9 * * 1"},"tarball_path":"https://example.com/x.tar.gz","entrypoint":"bash run.sh"}}' --expect 200 --check valid eq true
  ```
- **Advisory only (`F03.validate-advisory`).** An `http://` tarball passes validate, creation refuses it, and nothing was created either way.
  ```sh
  N=$(control-automation api GET /v1 --expect 200 --field total)
  control-automation api POST /v1/validate --json '{"endpoint":"/v1","draft":{"name":"qa-advisory","trigger":{"type":"cron","schedule":"0 9 * * 1"},"tarball_path":"http://example.com/x.tar.gz","entrypoint":"bash run.sh"}}' --expect 200 --check valid eq true --save F03.validate-advisory/validate
  control-automation api POST /v1 --json '{"name":"qa-advisory","trigger":{"type":"cron","schedule":"0 9 * * 1"},"tarball_path":"http://example.com/x.tar.gz","entrypoint":"bash run.sh"}' --expect 422 --check detail contains 'Invalid tarball_path' --save F03.validate-advisory/create
  control-automation api GET /v1 --expect 200 --check total eq "$N" --quiet
  ```
- **Built-in sources (`F03.caps-builtin-sources`) and GitHub event types (`F03.validate-event-type`).** Restart with the built-in webhook secret, read capabilities, validate a GitHub pattern that cannot be delivered, then restore.
  ```sh
  control-automation restart --service automation --env AUTOMATION_WEBHOOK_SECRET=qa-builtin-secret
  control-automation api GET /v1/capabilities --expect 200 --quiet --check eventSources eq '["bitbucket_data_center", "github", "jira_dc"]' --check triggerKinds eq '["cron", "event"]' --check eventTypes contains push --check eventTypes contains 'pull_request.*' --check features contains webhookDelivery --save F03.caps-builtin-sources/capabilities
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"github","on":"nonexistent.*"}}}' --expect 200 --check valid eq false --check errors.0.field eq trigger.on --check errors.0.code eq event_type_not_delivered --check errors.0.message eq "This deployment cannot parse 'nonexistent.*' events from GitHub." --save F03.validate-event-type/nonexistent
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"event","source":"github","on":"pull_request.opened"}}}' --expect 200 --check valid eq true
  control-automation restart --service automation --reset-env
  ```
- **Config moves the numbers (`F03.caps-config`) and the cron floor (`F03.validate-interval-too-short`).** A 120 s scheduler, a 900 s ceiling and no KV secret; a once-a-minute schedule is now too fast. Restore afterwards.
  ```sh
  control-automation restart --service automation --env AUTOMATION_SCHEDULER_INTERVAL_SECONDS=120 --env AUTOMATION_MAX_RUN_DURATION=900 --env AUTOMATION_KV_SECRET=
  control-automation api GET /v1/capabilities --expect 200 --quiet --check triggers.cron.minIntervalSeconds eq 120 --check maxAutomationTimeoutSeconds eq 900 --check features not-contains kvStore --check features contains customTarball --save F03.caps-config/capabilities
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"cron","schedule":"*/1 * * * *"}}}' --expect 200 --check valid eq false --check errors.0.field eq trigger.schedule --check errors.0.code eq interval_too_short --check errors.0.message eq 'This deployment fires an automation at most once every 120 seconds.' --save F03.validate-interval-too-short/every-minute
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","trigger":{"type":"cron","schedule":"*/2 * * * *"}}}' --expect 200 --check valid eq true
  control-automation api POST /v1/validate --json '{"endpoint":"/v1/preset/prompt","draft":{"name":"qa","prompt":"hi","timeout":1000,"trigger":{"type":"cron","schedule":"0 0 1 1 *"}}}' --expect 200 --check errors.0.message contains 'timeout must not exceed 900 seconds (15 minutes)'
  control-automation restart --service automation --reset-env
  control-automation api GET /v1/capabilities --expect 200 --quiet --check triggers.cron.minIntervalSeconds eq 60 --check features contains kvStore
  ```
  On the default floor of 60 s the same `*/1 * * * *` (or `* * * * *`) is
  valid: five-field cron cannot fire faster than once a minute.
- **Cloud mode without a service key (`F03.caps-not-ready`).** Empty `AUTOMATION_AGENT_SERVER_URL` turns local mode off, so auth goes upstream: a users/me stub answers for the run's key. Restore afterwards.
  ```sh
  UM=$(control-automation fixture http-sink --name usersme --body '{"id":"11111111-1111-4111-8111-111111111111","org_id":"22222222-2222-4222-8222-222222222222","email":"qa-other@example.test","role":"member","permissions":["view_automations","manage_automations"]}' --field url)
  control-automation restart --service automation --env AUTOMATION_AGENT_SERVER_URL= --env "AUTOMATION_OPENHANDS_API_BASE_URL=$UM"
  control-automation api GET /v1/capabilities --expect 200 --check ready eq false --check triggerKinds len-eq 0 --check eventSources len-eq 0 --check eventTypes len-eq 0 --check features len-eq 0 --check repoProviders len-eq 0 --check triggers eq '{}' --check maxAutomationTimeoutSeconds eq 1800 --save F03.caps-not-ready/capabilities
  control-automation restart --service automation --reset-env
  control-automation api GET /v1/capabilities --expect 200 --quiet --check ready eq true
  ```

## Gotchas

- An invalid draft is a 200 with `valid:false`; only the envelope (bad
  `endpoint`, unknown top-level key, missing `draft`) is a 422. Check
  `valid`, not the status.
- Schema errors short-circuit: semantic codes (`interval_too_short`,
  `event_source_not_configured`, `event_type_not_delivered`,
  `invalid_agent_profile`) appear only once the draft's shape is valid.
- `interval_too_short` is unreachable on a default deployment: the floor is
  60 s and five-field cron fires at most once a minute. It shows only when
  `AUTOMATION_SCHEDULER_INTERVAL_SECONDS` is above 60.
- Validate is advisory: it does not check `tarball_path` scheme strictness
  (`http://` passes), upload ownership or existence; creation does.
- Custom webhook sources are per organization and only while enabled; the
  built-in three need `AUTOMATION_WEBHOOK_SECRET`. `eventTypes` comes from
  GitHub's catalog only, so a custom-only deployment reports `[]`.
- `sampleEventMatched` is `null` (present, not missing) unless the trigger
  is an event trigger on a configured source and the sample parses.
- In local mode the agent-profile lookup and the model-profile check are
  skipped; `model_profile_not_found` and `Agent profile ... not found` are
  SaaS-only codes.
- The response models are camelCase; a snake_case field such as
  `max_automation_timeout_seconds` is absent. The draft itself stays
  snake_case.
- `restart --reset-env` drops every `--env`, including launch-time ones.

# Event ingestion and triggers

How an external event becomes automation runs. A sender POSTs to
`/v1/events/{org_id}/{source}` with no API key: the signature is the
authentication, checked with the custom webhook's secret and scheme
(`hmac_sha256_hex`, `standard_webhooks` or `slack_v0`, F10 configures them) or,
for the built-ins `github`, `jira_dc` and `bitbucket_data_center`, with the
deployment-wide `AUTOMATION_WEBHOOK_SECRET`. The service derives an event key
(the custom webhook's JMESPath `event_key_expr`, or the provider's detection),
records the event, deduplicates it on the delivery-id header, matches every
enabled event automation of that org and source on `on` and the optional
JMESPath `filter`, and creates one PENDING run per match (`trigger_source:
event`) whose script receives the event in `AUTOMATION_EVENT_PAYLOAD`. A
`continue_conversation` trigger folds later events about the same subject into
that run instead. Built-in forwarders also read
`/v1/events/{source}/requested-types` to learn which event types to forward.

Source: `openhands/automation/event_router.py`, `openhands/automation/ingest.py`, `openhands/automation/providers.py`, `openhands/automation/trigger_matcher.py`, `openhands/automation/filter_eval.py`, `openhands/automation/conversations.py`, `openhands/automation/subjects.py`, `openhands/automation/utils/webhook.py`, `openhands/automation/event_schemas/`, `openhands/automation/dispatcher.py`

Launch: `--env AUTOMATION_WEBHOOK_SECRET=qa-builtin-secret`

Routes: `POST /api/automation/v1/events/{org_id}/{source}`,
`GET /api/automation/v1/events/{source}/requested-types`

## Sub-features

- `F11.no-match`: a correctly signed custom delivery with no matching automation answers 200 `{received:true, matched:0, runs_created:[], conversations_continued:[]}` and is still recorded in `integration_events` with `matched_count` 0.
- `F11.hmac-signature`: `hmac_sha256_hex` accepts `sha256=<hex>` and bare hex; a tampered signature or a wrong secret answers 401 `Invalid signature`, and an unsigned delivery 401 `Missing signature header: X-Signature-256`; no API key is needed.
- `F11.standard-webhooks`: a `standard_webhooks` delivery verifies with a server-generated `whsec_` secret (used whole as the key) and with a base64 `whsec_` secret; a timestamp older than 300 s, a missing `webhook-id` or a tampered signature answers 401 `Invalid signature`.
- `F11.slack-v0`: a `slack_v0` delivery (`v0=` signature over `v0:<ts>:<body>` plus `X-Slack-Request-Timestamp`) is accepted; a stale or missing timestamp, or an `hmac_sha256_hex` signature in the Slack header, answers 401.
- `F11.unknown-source`: an unknown source, or a custom source posted under another org id, answers 404 `Unknown webhook source: <source>`; a non-UUID org id answers 422.
- `F11.bad-body`: a body that is not JSON answers 400 `Invalid JSON`, a JSON array 400 `Failed to parse event`, and a payload the `event_key_expr` finds nothing in answers 200 `matched:0` without recording the event.
- `F11.match-run`: an automation with `on: "order.*"` matches `order.created`: `matched:1`, one id in `runs_created`, a run with `trigger_source: event` that completes, and an `AUTOMATION_EVENT_PAYLOAD` whose `event` is the custom wrapper `{payload, source_override, event_key}`.
- `F11.on-list`: `on` given as a list matches any listed key exactly and nothing else.
- `F11.filter`: a JMESPath `filter` (`amount > \`100\``) on the raw payload gates the match.
- `F11.filter-functions`: the custom filter functions (`icontains`, `regex`, `starts_with`, `lower`) work in a filter.
- `F11.filter-runtime-error`: a filter that raises at evaluation (`glob` on a missing field) counts as no match (200, `matched:0`, a logged warning), and the same automation matches once the field is present.
- `F11.filter-invalid`: an automation whose `filter` or `subject_key_expr` does not compile is refused with 422 at creation.
- `F11.nested-key`: `event_key_expr: event.name` reads a nested key, and a numeric key becomes the string `"42"`.
- `F11.dedupe`: with `event_id_header` set, a second delivery with the same id answers `matched:0` and creates no run, a new id matches again; without a delivery-id header every delivery creates a run.
- `F11.disabled-automation`: a disabled automation is not matched; re-enabled, it is.
- `F11.builtin-github`: with `AUTOMATION_WEBHOOK_SECRET`, a signed `{payload: <pull_request opened>}` matches `on: pull_request.opened` with a filter, a redelivery with the same `X-GitHub-Delivery` matches nothing, a body without `payload` answers 400, an unsupported GitHub event (`workflow_run`) answers 200 `matched:0`, and the run's `event` is the parsed model with `event_key`.
- `F11.builtin-jira-bitbucket`: `jira_dc` keys on `payload.webhookEvent` and `bitbucket_data_center` on `payload.eventKey`; a jira payload without `webhookEvent` answers 200 `matched:0`.
- `F11.requested-types`: `github` lists its 6 parseable event types and 8 detection rules, `jira_dc` lists the `on` patterns of enabled automations (`*` collapses to `["*"]`), a custom source answers 404, and a tampered or missing signature 401.
- `F11.builtin-unconfigured`: without `AUTOMATION_WEBHOOK_SECRET` the built-ins answer 404 on both routes, even correctly signed; with it back, they answer 200.
- `F11.continue-conversation`: with `destination: continue_conversation`, the first event on a subject creates a run, a second event on the same subject while that run is still queued answers `runs_created:[]` and `conversations_continued:[<uuid5>]`, an event without a subject creates an ordinary run, and the run starts with `AUTOMATION_CONVERSATION_ID` set to that id and the second event in `follow_up_turns`.

## How to get to it (agent POV)

- REST: `POST /api/automation/v1/events/{org_id}/{source}` is what an external
  service (or the OpenHands server, for the built-ins) calls; it takes no API
  key. Every recipe sends it with the `event send` verb, which signs
  the exact bytes it sends (`--scheme`, `--secret`, `--builtin` for the
  `{payload: ...}` wrapper and the built-in secret, `--tamper`, `--bare-hex`,
  `--unsigned`, `--age`, `--drop-header`, `--delivery-id`).
- `GET /api/automation/v1/events/{source}/requested-types` is read by trusted
  forwarders, signed over the source name; recipes use the
  `event requested-types` verb.
- Event automations: a `trigger` of `{"type":"event","source":...,"on":...,"filter":...,"destination":...,"subject_key_expr":...}`
  on `POST /v1` (here through `fixture automation --event-source`) or on the
  preset routes. Creation does not check that the source is configured; the
  preflight (`POST /v1/validate`) and `GET /v1/capabilities` do, and they are
  mapped in F03.
- The run's script reads `AUTOMATION_EVENT_PAYLOAD` (and, for
  `continue_conversation`, `AUTOMATION_CONVERSATION_ID`); the callback
  fixture dumps both to `qa-env.json` in the run's workspace, which is the
  second view used here. The runs list (`GET /v1/{id}/runs`) shows
  `trigger_source: event` but not the payload.
- Config: `AUTOMATION_WEBHOOK_SECRET` turns the three built-ins on (one shared
  secret); `AUTOMATION_INTEGRATION_EVENT_RETENTION_DAYS` (default 14) bounds the
  dedupe window.

## Driving it with control-automation

Preconditions:

- A run launched with `--env AUTOMATION_WEBHOOK_SECRET=qa-builtin-secret`
  (the family's `Launch:` line; `event send --builtin` and
  `event requested-types` read it from the run). No LLM, no network.
- A custom webhook `qa-orders` with the supplied secret `qa-orders-secret`
  (default scheme `hmac_sha256_hex`, header `X-Signature-256`, key `type`),
  used by most bullets:
  ```sh
  control-automation api POST /v1/webhooks --json '{"name":"qa orders","source":"qa-orders","webhook_secret":"qa-orders-secret"}' --expect 201 --check webhook_secret eq null
  ```
- Event automations are callback fixtures (`fixture automation --kind callback --event-source ...`):
  each run dumps its environment to `qa-env.json` and completes through the
  callback within seconds.

- **No matching automation (`F11.no-match`).** Accepted and recorded, nothing matched.
  ```sh
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"misc.noop"}' --expect 200 --check received eq true --check matched eq 0 --check runs_created len-eq 0 --check conversations_continued len-eq 0 --save F11.no-match/send
  control-automation state sql "SELECT matched_count, provider_event_id FROM integration_events WHERE source = 'qa-orders' AND event_key = 'misc.noop'" --check count eq 1 --check rows.0.matched_count eq 0 --check rows.0.provider_event_id eq null
  ```
- **HMAC signatures (`F11.hmac-signature`).** Both header forms verify; anything else is refused before the body is read.
  ```sh
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"misc.prefixed"}' --expect 200 --save F11.hmac-signature/prefixed
  control-automation event send qa-orders --secret qa-orders-secret --bare-hex --json '{"type":"misc.bare"}' --expect 200 --save F11.hmac-signature/bare-hex
  control-automation event send qa-orders --secret qa-orders-secret --tamper --json '{"type":"misc.tampered"}' --expect 401 --check detail eq 'Invalid signature' --save F11.hmac-signature/tampered
  control-automation event send qa-orders --secret qa-not-the-secret --json '{"type":"misc.wrong"}' --expect 401 --check detail eq 'Invalid signature'
  control-automation event send qa-orders --secret qa-orders-secret --unsigned --json '{"type":"misc.unsigned"}' --expect 401 --check detail eq 'Missing signature header: X-Signature-256'
  control-automation state sql "SELECT count(*) AS n FROM integration_events WHERE event_key IN ('misc.tampered','misc.wrong','misc.unsigned')" --check rows.0.n eq 0
  ```
  The two good deliveries answer 200; the three refused ones answer 401 and
  leave no `integration_events` row.
- **Standard Webhooks (`F11.standard-webhooks`).** Generated and base64 secrets both verify; replay protection refuses stale or id-less deliveries.
  ```sh
  STD_SECRET=$(control-automation api POST /v1/webhooks --json '{"name":"qa std","source":"qa-std","signature_scheme":"standard_webhooks","signature_header":"webhook-signature"}' --expect 201 --field webhook_secret --unsafe-unredacted)
  control-automation event send qa-std --scheme standard_webhooks --signature-header webhook-signature --secret "$STD_SECRET" --json '{"type":"std.ok"}' --expect 200 --check received eq true --save F11.standard-webhooks/generated
  control-automation event send qa-std --scheme standard_webhooks --signature-header webhook-signature --secret "$STD_SECRET" --age 600 --json '{"type":"std.stale"}' --expect 401 --check detail eq 'Invalid signature' --save F11.standard-webhooks/stale
  control-automation event send qa-std --scheme standard_webhooks --signature-header webhook-signature --secret "$STD_SECRET" --drop-header webhook-id --json '{"type":"std.noid"}' --expect 401 --check detail eq 'Invalid signature'
  control-automation event send qa-std --scheme standard_webhooks --signature-header webhook-signature --secret "$STD_SECRET" --tamper --json '{"type":"std.tampered"}' --expect 401
  control-automation event send qa-std --scheme standard_webhooks --secret "$STD_SECRET" --json '{"type":"std.wrongheader"}' --expect 401 --check detail eq 'Missing signature header: webhook-signature'
  control-automation api POST /v1/webhooks --json '{"name":"qa std64","source":"qa-std64","signature_scheme":"standard_webhooks","signature_header":"webhook-signature","webhook_secret":"whsec_cWEtc3RkLWtleS0xMjM0NTY="}' --expect 201 --quiet
  control-automation event send qa-std64 --scheme standard_webhooks --signature-header webhook-signature --secret 'whsec_cWEtc3RkLWtleS0xMjM0NTY=' --json '{"type":"std.b64"}' --expect 200
  control-automation event send qa-std64 --scheme standard_webhooks --signature-header webhook-signature --secret 'whsec_dWEtc3RkLWtleS0xMjM0NTY=' --json '{"type":"std.b64wrong"}' --expect 401
  ```
  A fresh timestamp passes; 600 s old, without `webhook-id`, or tampered it
  is 401. The base64 secret works and a one-character change to it does not
(change a leading character: the last one before `=` can be padding bits that
decode to the same key).
- **Slack v0 (`F11.slack-v0`).** The timestamp is part of the signed string and must be fresh.
  ```sh
  control-automation api POST /v1/webhooks --json '{"name":"qa slack v0","source":"qa-slk","signature_scheme":"slack_v0","signature_header":"X-Slack-Signature","webhook_secret":"qa-slack-secret"}' --expect 201 --quiet
  control-automation event send qa-slk --scheme slack_v0 --secret qa-slack-secret --json '{"type":"slk.ok"}' --expect 200 --check received eq true --save F11.slack-v0/ok
  control-automation event send qa-slk --scheme slack_v0 --secret qa-slack-secret --age 600 --json '{"type":"slk.stale"}' --expect 401 --check detail eq 'Invalid signature' --save F11.slack-v0/stale
  control-automation event send qa-slk --scheme slack_v0 --secret qa-slack-secret --drop-header X-Slack-Request-Timestamp --json '{"type":"slk.nots"}' --expect 401 --check detail eq 'Invalid signature'
  control-automation event send qa-slk --scheme hmac_sha256_hex --signature-header X-Slack-Signature --secret qa-slack-secret --json '{"type":"slk.hmac"}' --expect 401 --check detail eq 'Invalid signature'
  ```
- **Unknown source or org (`F11.unknown-source`).** A custom source exists only under its own org.
  ```sh
  control-automation event send qa-nope --secret qa-orders-secret --json '{"type":"x"}' --expect 404 --check detail eq 'Unknown webhook source: qa-nope' --save F11.unknown-source/source
  control-automation event send qa-orders --org 00000000-0000-0000-0000-000000000001 --secret qa-orders-secret --json '{"type":"x"}' --expect 404 --check detail eq 'Unknown webhook source: qa-orders' --save F11.unknown-source/other-org
  control-automation event send qa-orders --org not-a-uuid --secret qa-orders-secret --json '{"type":"x"}' --expect 422 --check detail.0.loc contains org_id
  ```
- **Bodies that cannot be routed (`F11.bad-body`).** Malformed bodies are 400; a body without the event key is acknowledged but not recorded.
  ```sh
  N_EV=$(control-automation state sql "SELECT count(*) AS n FROM integration_events WHERE source = 'qa-orders'" --field rows.0.n)
  control-automation event send qa-orders --secret qa-orders-secret --data 'not-json' --expect 400 --check detail contains 'Invalid JSON' --save F11.bad-body/not-json
  control-automation event send qa-orders --secret qa-orders-secret --json '[1,2]' --expect 400 --check detail contains 'Failed to parse event'
  control-automation event send qa-orders --secret qa-orders-secret --json '{"kind":"no type field"}' --expect 200 --check matched eq 0 --check runs_created len-eq 0 --save F11.bad-body/no-key
  control-automation state sql "SELECT count(*) AS n FROM integration_events WHERE source = 'qa-orders'" --check rows.0.n eq "$N_EV"
  control-automation logs --service automation --grep 'Could not extract event_key' --expect-min 1 --tail 1
  ```
  The no-key delivery answers 200 `matched:0` and adds no row; the log says
  why (`Could not extract event_key from payload using expression 'type'`).
- **A match creates a run (`F11.match-run`).** The run carries the event in its environment.
  ```sh
  AID=$(control-automation fixture automation --kind callback --name 'qa order run' --event-source qa-orders --on 'order.*' --field id)
  RID=$(control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"order.created","amount":5}' --expect 200 --check matched eq 1 --check runs_created len-eq 1 --save F11.match-run/send --field runs_created.0)
  control-automation run wait "$AID" --run-id "$RID" --status COMPLETED --timeout 120 --check trigger_source eq event --save F11.match-run/run
  control-automation state cat "workspaces/automation-runs/$RID/qa-env.json" --check AUTOMATION_RUN_ID eq "$RID"
  control-automation state cat "workspaces/automation-runs/$RID/qa-env.json" --field AUTOMATION_EVENT_PAYLOAD | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["trigger"] == "event" and p["trigger_payload"]["on"] == "order.*", p; assert p["event"] == {"payload": {"type": "order.created", "amount": 5}, "source_override": "qa-orders", "event_key": "order.created"}, p["event"]'
  control-automation state sql "SELECT matched_count FROM integration_events WHERE event_key = 'order.created'" --check rows.0.matched_count eq 1
  ```
  `run wait` reports `COMPLETED` with `trigger_source: event`; the run's
  `AUTOMATION_EVENT_PAYLOAD.event` is the wrapper, with the raw body under
  `payload`.
- **List of keys in `on` (`F11.on-list`).** Exact keys, no prefix matching.
  ```sh
  control-automation fixture automation --kind callback --name 'qa list on' --event-source qa-orders --extra-json '{"trigger":{"type":"event","source":"qa-orders","on":["alpha.done","beta.done"]}}' --field id
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"beta.done"}' --expect 200 --check matched eq 1 --save F11.on-list/listed
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"alpha.done"}' --expect 200 --check matched eq 1
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"beta.other"}' --expect 200 --check matched eq 0
  ```
- **Filter (`F11.filter`).** The same event key matches only when the filter holds.
  ```sh
  control-automation fixture automation --kind callback --name 'qa big refund' --event-source qa-orders --on 'refund.created' --filter 'amount > `100`' --field id
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"refund.created","amount":50}' --expect 200 --check matched eq 0 --save F11.filter/below
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"refund.created","amount":500}' --expect 200 --check matched eq 1 --check runs_created len-eq 1 --save F11.filter/above
  ```
- **Custom filter functions (`F11.filter-functions`).** `icontains`, `regex`, `starts_with` and `lower` are available to filters.
  ```sh
  control-automation fixture automation --kind callback --name 'qa fn filter' --event-source qa-orders --on 'note.added' --filter "icontains(text, 'urgent') && regex(ref, '^refs/heads/rel-') && starts_with(lower(author), 'qa')" --field id
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"note.added","text":"Very URGENT","ref":"refs/heads/rel-1","author":"QA-Bot"}' --expect 200 --check matched eq 1 --save F11.filter-functions/match
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"note.added","text":"Very URGENT","ref":"refs/heads/main","author":"QA-Bot"}' --expect 200 --check matched eq 0
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"note.added","text":"routine","ref":"refs/heads/rel-1","author":"QA-Bot"}' --expect 200 --check matched eq 0
  ```
- **Filter that fails at runtime (`F11.filter-runtime-error`).** No match and no 500; the same filter matches when the field exists.
  ```sh
  control-automation fixture automation --kind callback --name 'qa glob filter' --event-source qa-orders --on 'tag.pushed' --filter "glob(name, 'v*')" --field id
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"tag.pushed"}' --expect 200 --check matched eq 0 --save F11.filter-runtime-error/missing-field
  control-automation logs --service automation --grep 'Filter evaluation failed: .*glob' --expect-min 1 --tail 1
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"tag.pushed","name":"v1.2"}' --expect 200 --check matched eq 1 --save F11.filter-runtime-error/field-present
  ```
- **Filter that does not compile (`F11.filter-invalid`).** Refused at creation, field named.
  ```sh
  TP=$(control-automation fixture tarball --kind callback --upload --field tarball_path)
  control-automation api POST /v1 --json "{\"name\":\"qa bad filter\",\"trigger\":{\"type\":\"event\",\"source\":\"qa-orders\",\"on\":\"x\",\"filter\":\"a[[\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\"}" --expect 422 --check detail.0.msg contains 'Invalid filter expression' --save F11.filter-invalid/filter
  control-automation api POST /v1 --json "{\"name\":\"qa bad subject\",\"trigger\":{\"type\":\"event\",\"source\":\"qa-orders\",\"on\":\"x\",\"destination\":\"continue_conversation\",\"subject_key_expr\":\"a[[\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"bash run.sh\"}" --expect 422 --check detail.0.msg contains 'Invalid subject_key_expr expression'
  control-automation state sql "SELECT count(*) AS n FROM automations WHERE name LIKE 'qa bad %'" --check rows.0.n eq 0
  ```
- **Nested and numeric event keys (`F11.nested-key`).** `event_key_expr` is any JMESPath; non-string keys are stringified.
  ```sh
  control-automation api POST /v1/webhooks --json '{"name":"qa nested","source":"qa-nest","webhook_secret":"qa-nest-secret","event_key_expr":"event.name"}' --expect 201 --quiet
  control-automation fixture automation --kind callback --name 'qa nested key' --event-source qa-nest --on 'deploy.done' --field id
  control-automation fixture automation --kind callback --name 'qa numeric key' --event-source qa-nest --on '42' --field id
  control-automation event send qa-nest --secret qa-nest-secret --json '{"event":{"name":"deploy.done"}}' --expect 200 --check matched eq 1 --save F11.nested-key/nested
  control-automation event send qa-nest --secret qa-nest-secret --json '{"event":{"name":42}}' --expect 200 --check matched eq 1 --save F11.nested-key/numeric
  control-automation event send qa-nest --secret qa-nest-secret --json '{"type":"deploy.done"}' --expect 200 --check matched eq 0
  control-automation state sql "SELECT event_key FROM integration_events WHERE source = 'qa-nest' ORDER BY event_key" --check count eq 2 --check rows.0.event_key eq 42 --check rows.1.event_key eq deploy.done
  ```
  A top-level `type` means nothing to this webhook (no match, not recorded).
- **Delivery-id dedupe (`F11.dedupe`).** Only a configured delivery-id header deduplicates.
  ```sh
  control-automation api POST /v1/webhooks --json '{"name":"qa dedupe","source":"qa-dedupe","webhook_secret":"qa-dedupe-secret","event_id_header":"X-Qa-Delivery"}' --expect 201 --quiet
  DD_AID=$(control-automation fixture automation --kind callback --name 'qa dedupe' --event-source qa-dedupe --on 'ping' --field id)
  control-automation event send qa-dedupe --secret qa-dedupe-secret --delivery-id X-Qa-Delivery=qa-d-1 --json '{"type":"ping"}' --expect 200 --check matched eq 1 --check runs_created len-eq 1 --save F11.dedupe/first
  control-automation event send qa-dedupe --secret qa-dedupe-secret --delivery-id X-Qa-Delivery=qa-d-1 --json '{"type":"ping"}' --expect 200 --check matched eq 0 --check runs_created len-eq 0 --save F11.dedupe/redelivery
  control-automation event send qa-dedupe --secret qa-dedupe-secret --delivery-id X-Qa-Delivery=qa-d-2 --json '{"type":"ping"}' --expect 200 --check matched eq 1
  control-automation state sql "SELECT provider_event_id FROM integration_events WHERE source = 'qa-dedupe' ORDER BY provider_event_id" --check count eq 2 --check rows.0.provider_event_id eq qa-d-1 --check rows.1.provider_event_id eq qa-d-2
  control-automation api GET "/v1/$DD_AID/runs" --expect 200 --check total eq 2
  control-automation api POST /v1/webhooks --json '{"name":"qa no dedupe","source":"qa-nodedupe","webhook_secret":"qa-nodedupe-secret"}' --expect 201 --quiet
  ND_AID=$(control-automation fixture automation --kind callback --name 'qa no dedupe' --event-source qa-nodedupe --on 'ping' --field id)
  control-automation event send qa-nodedupe --secret qa-nodedupe-secret --delivery-id X-Qa-Delivery=qa-d-1 --json '{"type":"ping"}' --expect 200 --check matched eq 1
  control-automation event send qa-nodedupe --secret qa-nodedupe-secret --delivery-id X-Qa-Delivery=qa-d-1 --json '{"type":"ping"}' --expect 200 --check matched eq 1 --save F11.dedupe/no-header-configured
  control-automation api GET "/v1/$ND_AID/runs" --expect 200 --check total eq 2
  ```
  The redelivery of `qa-d-1` creates nothing (the automation has two runs, not
  three). `qa-nodedupe` names no delivery-id header, so the same header
  there is ignored and both deliveries create runs.
- **Disabled automation (`F11.disabled-automation`).** Only enabled automations are candidates.
  ```sh
  DIS_AID=$(control-automation fixture automation --kind callback --name 'qa disabled' --event-source qa-orders --on 'audit.logged' --field id)
  control-automation api PATCH "/v1/$DIS_AID" --json '{"enabled":false}' --expect 200 --check enabled eq false
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"audit.logged"}' --expect 200 --check matched eq 0 --save F11.disabled-automation/disabled
  control-automation api PATCH "/v1/$DIS_AID" --json '{"enabled":true}' --expect 200 --check enabled eq true
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"audit.logged"}' --expect 200 --check matched eq 1 --save F11.disabled-automation/enabled
  control-automation api GET "/v1/$DIS_AID/runs" --expect 200 --check total eq 1
  ```
- **Built-in GitHub (`F11.builtin-github`).** The forwarder's `{payload: ...}` wrapper, signed with the shared secret.
  ```sh
  PR='{"action":"opened","number":42,"pull_request":{"number":42,"title":"qa","state":"open","base":{"ref":"main","sha":"b1"},"head":{"ref":"qa","sha":"h1"},"user":{"id":1,"login":"qa"}},"repository":{"id":1,"name":"r","full_name":"qa/r","private":false},"sender":{"id":1,"login":"qa"}}'
  GH_AID=$(control-automation fixture automation --kind callback --name 'qa gh pr' --event-source github --on 'pull_request.opened' --filter "pull_request.base.ref == 'main'" --field id)
  GH_RID=$(control-automation event send github --builtin --delivery-id X-GitHub-Delivery=qa-gh-1 --json "$PR" --expect 200 --check matched eq 1 --save F11.builtin-github/pr-opened --field runs_created.0)
  control-automation event send github --builtin --delivery-id X-GitHub-Delivery=qa-gh-1 --json "$PR" --expect 200 --check matched eq 0 --save F11.builtin-github/redelivery
  control-automation event send github --builtin --tamper --json "$PR" --expect 401 --check detail eq 'Invalid signature'
  control-automation event send github --secret qa-builtin-secret --signature-header X-Hub-Signature-256 --json "$PR" --expect 400 --check detail eq 'Missing payload in builtin source request'
  control-automation event send github --builtin --json '{"workflow_run":{"id":1},"repository":{"id":1,"name":"r","full_name":"qa/r","private":false},"sender":{"id":1,"login":"qa"}}' --expect 200 --check matched eq 0
  control-automation run wait "$GH_AID" --run-id "$GH_RID" --status COMPLETED --timeout 120 --check trigger_source eq event
  control-automation state cat "workspaces/automation-runs/$GH_RID/qa-env.json" --field AUTOMATION_EVENT_PAYLOAD | python3 -c 'import json,sys; e=json.load(sys.stdin)["event"]; assert e["event_key"] == "pull_request.opened" and e["pull_request"]["base"]["ref"] == "main" and e["repository"]["default_branch"] == "main" and "payload" not in e, e'
  ```
  The redelivery with the same `X-GitHub-Delivery` matches nothing; the raw
  PR without the wrapper is a 400; `workflow_run` is acknowledged with
  `matched:0`. The run's `event` is the parsed PR model (with filled-in
  defaults such as `default_branch`), not a `{payload: ...}` wrapper.
- **Built-in Jira and Bitbucket Data Center (`F11.builtin-jira-bitbucket`).** Each keys on its own field.
  ```sh
  control-automation fixture automation --kind callback --name 'qa jira' --event-source jira_dc --on 'jira:issue_*' --field id
  control-automation fixture automation --kind callback --name 'qa bitbucket' --event-source bitbucket_data_center --on 'pr:opened' --field id
  control-automation event send jira_dc --builtin --json '{"webhookEvent":"jira:issue_created","issue":{"key":"QA-1"}}' --expect 200 --check matched eq 1 --save F11.builtin-jira-bitbucket/jira
  control-automation event send jira_dc --builtin --json '{"issue":{"key":"QA-1"}}' --expect 200 --check matched eq 0
  control-automation event send bitbucket_data_center --builtin --json '{"eventKey":"pr:opened"}' --expect 200 --check matched eq 1 --save F11.builtin-jira-bitbucket/bitbucket
  control-automation event send bitbucket_data_center --builtin --json '{"eventKey":"pr:merged"}' --expect 200 --check matched eq 0
  ```
- **Requested event types (`F11.requested-types`).** Signed over the source name.
  ```sh
  control-automation event requested-types github --expect 200 --check event_types len-eq 6 --check event_types contains pull_request --check event_types contains release --check event_detection_rules len-eq 8 --save F11.requested-types/github
  control-automation event requested-types github --tamper --expect 401 --check detail eq 'Invalid signature'
  control-automation event requested-types github --unsigned --expect 401 --check detail eq 'Missing signature header: X-Hub-Signature-256'
  control-automation event requested-types qa-orders --expect 404 --check detail eq 'Unknown builtin webhook source: qa-orders'
  control-automation event requested-types bitbucket_data_center --expect 200 --check event_detection_rules len-eq 0
  RT_AID=$(control-automation fixture automation --kind callback --name 'qa jira types' --event-source jira_dc --extra-json '{"trigger":{"type":"event","source":"jira_dc","on":["jira:worklog_updated","jira:issue_deleted"]}}' --field id)
  control-automation event requested-types jira_dc --expect 200 --check event_types contains 'jira:worklog_updated' --check event_types contains 'jira:issue_deleted' --check event_detection_rules len-eq 0 --save F11.requested-types/jira
  control-automation api PATCH "/v1/$RT_AID" --json '{"trigger":{"type":"event","source":"jira_dc","on":"*"}}' --expect 200
  control-automation event requested-types jira_dc --expect 200 --check event_types len-eq 1 --check event_types.0 eq '*'
  control-automation api PATCH "/v1/$RT_AID" --json '{"enabled":false}' --expect 200
  control-automation event requested-types jira_dc --expect 200 --field event_types | python3 -c 'import json,sys; t=json.load(sys.stdin); assert "*" not in t and "jira:worklog_updated" not in t, t'
  ```
  `github` always lists its six parseable families. `jira_dc` lists the `on`
  patterns of enabled automations (no splitting on `:`), a `*` pattern
  collapses the list to `["*"]`, and disabling that automation removes its
  patterns again.
- **Built-ins without the shared secret (`F11.builtin-unconfigured`).** Restart with an empty secret, probe, then restore it.
  ```sh
  control-automation restart --service automation --env AUTOMATION_WEBHOOK_SECRET=
  control-automation event send github --secret qa-builtin-secret --signature-header X-Hub-Signature-256 --json '{"payload":{"zen":"qa"}}' --expect 404 --check detail eq 'Unknown webhook source: github' --save F11.builtin-unconfigured/event
  control-automation event requested-types github --secret qa-builtin-secret --expect 404 --check detail eq 'Unknown builtin webhook source: github'
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"misc.still-custom"}' --expect 200
  control-automation restart --service automation --env AUTOMATION_WEBHOOK_SECRET=qa-builtin-secret
  control-automation event send github --builtin --json '{"zen":"qa"}' --expect 200 --check matched eq 0
  control-automation event requested-types github --expect 200
  ```
  With the secret empty, the same correctly signed requests get 404 while the
  custom webhook keeps working; restoring the secret brings the built-ins back.
- **Continue a conversation (`F11.continue-conversation`).** Slow the dispatcher so the first run stays queued, send three events, then let it run.
  ```sh
  control-automation restart --service automation --env AUTOMATION_DISPATCHER_INTERVAL_SECONDS=3600
  CC_AID=$(control-automation fixture automation --kind callback --name 'qa conversation' --event-source qa-orders --on 'thread.*' --extra-json '{"trigger":{"type":"event","source":"qa-orders","on":"thread.*","destination":"continue_conversation","subject_key_expr":"thread"}}' --field id)
  control-automation api GET "/v1/$CC_AID" --expect 200 --check keep_alive eq true
  CC_CONV=$(python3 -c "import sys,uuid; print(uuid.uuid5(uuid.UUID('d7f3a2b1-5c48-4e9a-9b6d-2f1e8c3a7d40'), '46d09c5c-7d7d-585f-8c37-a6b45a3de738/' + sys.argv[1] + '/qa-orders/qa-t-1'))" "$CC_AID")
  CC_RID=$(control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"thread.msg","thread":"qa-t-1","text":"first"}' --expect 200 --check matched eq 1 --check runs_created len-eq 1 --save F11.continue-conversation/first --field runs_created.0)
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"thread.msg","thread":"qa-t-1","text":"second"}' --expect 200 --check matched eq 1 --check runs_created len-eq 0 --check conversations_continued.0 eq "$CC_CONV" --save F11.continue-conversation/coalesced
  control-automation event send qa-orders --secret qa-orders-secret --json '{"type":"thread.msg","text":"no subject"}' --expect 200 --check matched eq 1 --check runs_created len-eq 1 --check conversations_continued len-eq 0
  control-automation state sql "SELECT subject_key, started_at FROM automation_runs WHERE automation_id = '$(echo "$CC_AID" | tr -d -)' ORDER BY created_at" --check count eq 2 --check rows.0.subject_key eq qa-t-1 --check rows.0.started_at eq null --check rows.1.subject_key eq null
  control-automation restart --service automation --reset-env --env AUTOMATION_WEBHOOK_SECRET=qa-builtin-secret
  control-automation run wait "$CC_AID" --run-id "$CC_RID" --status COMPLETED --timeout 120 --save F11.continue-conversation/run
  control-automation state cat "workspaces/automation-runs/$CC_RID/qa-env.json" --check AUTOMATION_CONVERSATION_ID eq "$CC_CONV"
  control-automation state cat "workspaces/automation-runs/$CC_RID/qa-env.json" --field AUTOMATION_EVENT_PAYLOAD | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["event"]["payload"]["text"] == "first", p; t=p["follow_up_turns"]; assert len(t) == 1 and "second" in t[0], t'
  ```
  The second event on `qa-t-1` reports the derived conversation id instead of
  a run, the subject-less one gets its own run, and once the dispatcher is
  back to normal the first run starts with `AUTOMATION_CONVERSATION_ID` equal
  to that id and the second event as its only follow-up turn. The last
  restart puts the family's launch environment back.

## Gotchas

- `matched:0` means several things over HTTP: no automation matched, a
  duplicate delivery id, an event the provider cannot parse, or a filter that
  raised. Tell them apart with a matching automation and the
  `integration_events` table: a parse failure (no event key, unsupported
  GitHub event) records no row, a duplicate records no second row, and a plain
  no-match records a row with `matched_count` 0.
- Filters see the raw body (for built-ins, the inside of `payload`), but the
  run's `AUTOMATION_EVENT_PAYLOAD.event` differs by source: the
  `{payload, source_override, event_key}` wrapper for custom webhooks, the
  parsed provider model for GitHub (only modeled fields survive), and
  `{payload, ..., event_key}` for `jira_dc` and `bitbucket_data_center`.
- `hmac_sha256_hex` deliveries can be replayed unless the webhook names an
  `event_id_header`; the built-in `github` dedupes on `X-GitHub-Delivery`,
  `jira_dc` and `bitbucket_data_center` not at all. The dedupe window is the
  event retention (`AUTOMATION_INTEGRATION_EVENT_RETENTION_DAYS`).
- The built-ins accept any org id in the URL (the secret is deployment-wide);
  runs are created only for that org's automations, so send to the local org
  (`event send` does by default).
- A server-generated `whsec_` secret is not valid base64 once the prefix is
  removed, so `standard_webhooks` falls back to the whole string, prefix
  included, as the HMAC key. Senders that strip `whsec_` and base64-decode
  (the Standard Webhooks libraries) will not verify against a generated
  secret; supply a base64 `whsec_` secret instead.
- `restart --reset-env` also drops `AUTOMATION_WEBHOOK_SECRET` from this run's
  launch environment, and `event send --builtin` reads the secret from that
  environment; pass `--env AUTOMATION_WEBHOOK_SECRET=qa-builtin-secret` with
  any reset, as `F11.continue-conversation` does.
- The coalescing window is "created but not yet started". With the run's 2 s
  dispatcher it closes almost at once, which is why the recipe slows the
  dispatcher; in production (10 s) a burst of events usually lands in it. A
  turn for a started run is posted to the Agent Server conversation instead,
  and the callback fixture never opens one, so that branch falls back to a new
  run here.
- `requested-types` for `jira_dc` and `bitbucket_data_center` is not scoped
  to an org: it lists patterns from every org's enabled automations.
- Event routes have no rate limit and no API key; a wrong key or no key makes
  no difference to them.

# Stream sources (Slack Socket Mode)

Events that arrive over a connection the service holds open instead of an
inbound HTTP request; today only Slack Socket Mode. Configuring a Slack app in
`AUTOMATION_SLACK_APPS` starts a background supervisor that, per app, asserts
the bot token's identity with `auth.test`, opens the socket, acks every
envelope, and hands each `app_mention` from the configured team (bot messages
dropped) to the same `accept_event()` as a webhook, as source `slack`, event
key `app_mention`, deduplicated on the envelope's `event_id`. A failing source
is restarted with exponential backoff (5 s doubling, capped at 300 s); an
identity mismatch stops that source for good. There is no HTTP surface: the
supervisor shows itself only in the service log and in the runs it creates.
Without a real Slack app this family proves the lifecycle (idle, retrying,
killed, misconfigured) and, translated, the automation contract through a
custom webhook named `slack` that carries the same envelope.

Source: `openhands/automation/streams/`, `openhands/automation/config.py`, `openhands/automation/app.py`, `openhands/automation/ingest.py`

Routes: none

## Sub-features

- `F17.idle-without-apps`: with `AUTOMATION_SLACK_APPS` unset no stream supervisor starts (no `Background stream supervisor started` line) while the other loops do.
- `F17.fake-tokens-retry`: an app with fake tokens starts the supervisor (`Supervising 1 stream source(s): slack:<team_id>`), each `auth.test` failure is logged as `failed (n consecutive)` and retried after 5 s, then 10 s, and it is not treated as a terminal config error; the HTTP API keeps answering.
- `F17.kill-switch`: `AUTOMATION_STREAMS_ENABLED=false` keeps the supervisor down even with an app configured.
- `F17.malformed-apps`: an `AUTOMATION_SLACK_APPS` that is not JSON, or has an invalid `org_id`, makes the whole service fail at startup (exit 3, `error parsing value for field "slack_apps"`); removing it brings the service back.
- `F17.translated-webhook`: translated: a custom webhook named `slack` (`slack_v0`, `event_key_expr: event.type`) delivers a Slack `event_callback` envelope to an unmodified `source: slack, on: app_mention` automation; the filter reads top-level `team_id` and `event.text`, a non-mention event matches nothing, and the run's `event` is `{payload: <envelope>, source_override: slack, event_key: app_mention}`, the same shape the stream path emits.
- `F17.socket-mode-delivery`: blocked: a real Slack app's mention arrives over Socket Mode (`Slack Socket Mode connected for team=...`), creates one run, and a redelivered envelope with the same `event_id` creates none.
- `F17.identity-mismatch`: blocked: a valid bot token whose `auth.test` team or bot user differs from the configuration stops that source with `Stream source slack:<team> will not start: ...` and no retries.

## How to get to it (agent POV)

- Config only: `AUTOMATION_SLACK_APPS`, a JSON list of
  `{"org_id","app_token":"xapp-...","bot_token":"xoxb-...","team_id","bot_user_id"}`,
  and the kill switch `AUTOMATION_STREAMS_ENABLED` (default `true`). Both are
  read at startup; recipes set them with `restart --service automation --env`.
- Automations: an ordinary event trigger
  `{"type":"event","source":"slack","on":"app_mention","filter":...}`; nothing
  marks it as a stream automation, which is why the translated webhook path
  drives the same definition.
- Observation: the service log (`logs --grep ... --since-restart`) for the
  lifecycle, and the run's `qa-env.json` for the event it received.
  `stream_health()` (last connect, last event, consecutive failures) is
  process-local and served by no route.
- No SDK or TypeScript client method, and no Agent Canvas surface yet.

## Driving it with control-automation

Preconditions:

- A live run (`launch --new`) and a clean `doctor`. No LLM. The fake-token
  bullet makes the service call `https://slack.com/api/auth.test`; with
  outbound HTTPS it gets `invalid_auth`, without it a connection error, and
  both go down the same retry path.
- The two real-Slack bullets need a Slack app with Socket Mode enabled, its
  app-level token (`xapp-`, `connections:write`), its bot token (`xoxb-`,
  `app_mentions:read`), the workspace's `team_id` and the bot's user id, and a
  person to @-mention the bot. This machine has none, so they are blocked.
- Bullets that restart the service start with `--reset-env`, so each one sees
  only the variables it sets.

- **Idle without apps (`F17.idle-without-apps`).** The fresh run has no apps configured.
  ```sh
  control-automation logs --service automation --grep 'Watchdog started' --expect-min 1 --tail 1
  control-automation logs --service automation --grep 'stream supervisor|stream source' --expect-none
  ```
  The watchdog line is the positive control: the log is the right one and the
  lifespan ran; no stream line appears.
- **Fake tokens retry with backoff (`F17.fake-tokens-retry`).** Configure one app with tokens Slack rejects and wait for the second retry.
  ```sh
  control-automation restart --service automation --reset-env --env 'AUTOMATION_SLACK_APPS=[{"org_id":"46d09c5c-7d7d-585f-8c37-a6b45a3de738","app_token":"xapp-qa-fake","bot_token":"xoxb-qa-fake","team_id":"TQA","bot_user_id":"UQA"}]'
  control-automation logs --service automation --since-restart --grep 'Restarting stream source slack:TQA in 10s' --expect-min 1 --wait 120 --tail 1
  control-automation logs --service automation --since-restart --grep 'Background stream supervisor started' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'Supervising 1 stream source\(s\): slack:TQA' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'Stream source slack:TQA failed \(1 consecutive\)' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'Restarting stream source slack:TQA in 5s' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'Stream source slack:TQA failed \(2 consecutive\)' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'Restarting stream source slack:TQA in 10s' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'will not start' --expect-none
  control-automation api GET /v1 --expect 200 --check total ge 0 --save F17.fake-tokens-retry/api-still-up
  ```
  The supervisor starts, fails `auth.test`, restarts after 5 s, fails again and
  schedules the next try in 10 s (the doubling is the supervisor's own timer;
  `logs --wait` only polls the log for it). Nothing says `will not start`, and the
  REST API answers throughout.
- **Kill switch (`F17.kill-switch`).** The same app with streams disabled.
  ```sh
  control-automation restart --service automation --reset-env --env AUTOMATION_STREAMS_ENABLED=false --env 'AUTOMATION_SLACK_APPS=[{"org_id":"46d09c5c-7d7d-585f-8c37-a6b45a3de738","app_token":"xapp-qa-fake","bot_token":"xoxb-qa-fake","team_id":"TQA","bot_user_id":"UQA"}]'
  control-automation logs --service automation --since-restart --grep 'Watchdog started' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'stream supervisor|stream source' --expect-none
  control-automation api GET /ready --auth none --expect 200 --save F17.kill-switch/ready
  ```
- **Malformed app list (`F17.malformed-apps`).** The service refuses to start, then starts once the variable is gone.
  ```sh
  if control-automation restart --service automation --reset-env --env AUTOMATION_SLACK_APPS=not-json --timeout 60; then false; fi
  control-automation logs --service automation --since-restart --grep 'error parsing value for field .{0,4}slack_apps' --expect-min 1 --tail 1
  control-automation logs --service automation --since-restart --grep 'Application startup failed' --expect-min 1 --tail 1
  if control-automation restart --service automation --reset-env --env 'AUTOMATION_SLACK_APPS=[{"org_id":"not-a-uuid","app_token":"xapp-qa","bot_token":"xoxb-qa","team_id":"TQA","bot_user_id":"UQA"}]' --timeout 60; then false; fi
  control-automation logs --service automation --since-restart --grep 'Application startup failed' --expect-min 1 --tail 1
  control-automation restart --service automation --reset-env
  control-automation api GET /health --auth none --expect 200 --check status eq ok --save F17.malformed-apps/recovered
  ```
  Both restarts exit non-zero (`automation exited with code 3 during
  startup`); the log names the `slack_apps` field; the final restart without
  it is healthy.
- **Translated: a `slack` custom webhook drives a Slack automation (`F17.translated-webhook`).** Translated: Slack's HTTP Events API stands in for the socket; the envelope, source name, event key and automation are the ones the stream path uses.
  ```sh
  control-automation api POST /v1/webhooks --json '{"name":"qa slack events","source":"slack","signature_scheme":"slack_v0","signature_header":"X-Slack-Signature","event_key_expr":"event.type","webhook_secret":"qa-slack-signing-secret"}' --expect 201 --check source eq slack --save F17.translated-webhook/webhook
  SL_AID=$(control-automation fixture automation --kind callback --name 'qa slack mention' --event-source slack --on app_mention --filter "team_id == 'TQA' && icontains(event.text, 'deploy')" --field id)
  SL_RID=$(control-automation event send slack --scheme slack_v0 --secret qa-slack-signing-secret --json '{"token":"qa","team_id":"TQA","api_app_id":"AQA","type":"event_callback","event_id":"EvQA0001","event_time":1760000000,"event":{"type":"app_mention","user":"UHUMAN","text":"<@UQA> please Deploy","channel":"CQA","ts":"1760000000.000100"}}' --expect 200 --check matched eq 1 --check runs_created len-eq 1 --save F17.translated-webhook/mention --field runs_created.0)
  control-automation event send slack --scheme slack_v0 --secret qa-slack-signing-secret --json '{"token":"qa","team_id":"TOTHER","type":"event_callback","event_id":"EvQA0002","event":{"type":"app_mention","user":"UHUMAN","text":"<@UQA> deploy","channel":"CQA","ts":"1.2"}}' --expect 200 --check matched eq 0
  control-automation event send slack --scheme slack_v0 --secret qa-slack-signing-secret --json '{"token":"qa","team_id":"TQA","type":"event_callback","event_id":"EvQA0003","event":{"type":"message","user":"UHUMAN","text":"deploy","channel":"CQA","ts":"1.3"}}' --expect 200 --check matched eq 0
  control-automation event send slack --scheme slack_v0 --secret qa-slack-signing-secret --tamper --json '{"team_id":"TQA","event":{"type":"app_mention","text":"deploy"}}' --expect 401
  control-automation run wait "$SL_AID" --run-id "$SL_RID" --status COMPLETED --timeout 120 --check trigger_source eq event --save F17.translated-webhook/run
  control-automation state cat "workspaces/automation-runs/$SL_RID/qa-env.json" --field AUTOMATION_EVENT_PAYLOAD | python3 -c 'import json,sys; e=json.load(sys.stdin)["event"]; assert e["source_override"] == "slack" and e["event_key"] == "app_mention", e; assert e["payload"]["team_id"] == "TQA" and e["payload"]["event"]["text"] == "<@UQA> please Deploy", e'
  ```
  `slack` is not a reserved name, so the webhook is created. The mention from
  `TQA` matches and its run completes with the whole envelope under
  `event.payload`; the other team's mention and the plain `message` match
  nothing; a tampered `v0=` signature is 401.
- **Socket Mode delivery (`F17.socket-mode-delivery`), blocked.** Needs a real Slack app (prerequisites above).
  ```sh
  control-automation restart --service automation --reset-env --env "AUTOMATION_SLACK_APPS=[{\"org_id\":\"46d09c5c-7d7d-585f-8c37-a6b45a3de738\",\"app_token\":\"$QA_SLACK_APP_TOKEN\",\"bot_token\":\"$QA_SLACK_BOT_TOKEN\",\"team_id\":\"$QA_SLACK_TEAM_ID\",\"bot_user_id\":\"$QA_SLACK_BOT_USER_ID\"}]"
  control-automation logs --service automation --since-restart --grep 'Slack Socket Mode connected for team=' --expect-min 1
  SM_AID=$(control-automation fixture automation --kind callback --name 'qa socket mention' --event-source slack --on app_mention --field id)
  control-automation run wait "$SM_AID" --status COMPLETED --timeout 600 --check trigger_source eq event
  control-automation logs --service automation --since-restart --grep 'Stream source slack:.*app_mention matched 1 automation' --expect-min 1
  ```
  A person @-mentions the bot in a channel it is in after the automation is
  created; the run completes. A redelivery (Slack retries an envelope it did
  not see acked) carries the same `event_id` and creates no second run.
- **Identity mismatch (`F17.identity-mismatch`), blocked.** Needs a valid bot token (prerequisites above) configured with a wrong `team_id`.
  ```sh
  control-automation restart --service automation --reset-env --env "AUTOMATION_SLACK_APPS=[{\"org_id\":\"46d09c5c-7d7d-585f-8c37-a6b45a3de738\",\"app_token\":\"$QA_SLACK_APP_TOKEN\",\"bot_token\":\"$QA_SLACK_BOT_TOKEN\",\"team_id\":\"TNOTTHIS\",\"bot_user_id\":\"$QA_SLACK_BOT_USER_ID\"}]"
  control-automation logs --service automation --since-restart --grep 'Stream source slack:TNOTTHIS will not start: Slack token identifies team=' --expect-min 1
  control-automation logs --service automation --since-restart --grep 'Restarting stream source slack:TNOTTHIS' --expect-none
  ```
  Fake tokens cannot reach this path: `auth.test` must succeed for the
  identity comparison to run.

## Gotchas

- Both variables are read once at startup; changing them needs a restart, and
  a malformed `AUTOMATION_SLACK_APPS` takes the whole service down (REST
  included), not just the stream source. `restart --reset-env` is how the
  recipes clear it.
- A failing source never stops retrying (only an identity mismatch is
  terminal), so a deployment with a revoked token logs a traceback every 300 s
  at the cap. The failure count is process-local and resets on restart.
- The translated path is not the stream path in three ways: the webhook has
  no delivery-id header for Slack (the stream dedupes on the envelope's
  `event_id`, the webhook cannot, so a Slack HTTP retry creates a second run);
  team, bot-message and event-type drops happen only in the stream provider
  (over HTTP, the trigger's `on` and `filter` do that work, as the recipe's
  filter does for `team_id`); and Slack's HTTP `url_verification` handshake is
  not answered by the event route.
- A custom webhook named `slack` and a configured Slack app share the source
  name, so the same automation matches events from both transports.
- The fake-token run calls the real `slack.com` when outbound HTTPS is
  available; the tokens are obviously fake (`xoxb-qa-fake`) and Slack answers
  `invalid_auth`.

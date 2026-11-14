# Direct Teams outgoing webhooks

Teams can trigger Automation through a per-organization custom webhook. This
uses the event automation API and works in self-hosted deployments as well as
deployments exposing the same API. It does not require an Enterprise-only
integration or a native Teams application.

Outgoing webhooks support **@mentions in public (standard) Teams channels only**.
Conversational follow-ups are tracked in [extensions #924](https://github.com/OpenHands/extensions/issues/924).
See [Microsoft's Outgoing Webhook limitations](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-outgoing-webhook).

This server capability is tracked in [issue #714](https://github.com/OpenHands/automation/issues/714).
Canvas form controls and the reusable Teams example are separate follow-ups.
Use these API fields with a server version that includes this change.

## Direction of delivery

These are two independent webhook connections:

```text
Teams @mention -> Teams Outgoing Webhook -> Automation event -> pending run
Teams channel <- Teams Workflow callback <- automation script or other sender
```

An Outgoing Webhook sends a Teams message activity to Automation when someone
mentions it in a supported Teams channel. Automation verifies and admits the
activity, schedules matching automations, and returns a short acknowledgement.
The acknowledgement does not wait for the task to run.

Posting results back to Teams requires a separately configured Teams Workflow
callback or another supported posting mechanism. Configure that in the script;
the event endpoint does not automatically post a completion message. Store the
callback URL as a secret. This does not grant access to channel history, Teams
user impersonation, conversational follow-ups or thread continuation.

## Prerequisites

- An operator with `manage_automations` in the intended OpenHands organization.
- Permission to create a Teams Outgoing Webhook in a dedicated test team/channel.
  Availability depends on Teams organization policy and channel support.
- A public HTTPS Automation endpoint. Its ingress must preserve the request's
  raw bytes and `Authorization` header; an authentication proxy that replaces
  that header will break signature verification.
- An automation using `destination: dispatch_run`, the default. Deadline-bound
  webhook sources cannot be used with `continue_conversation` triggers.

The examples below use placeholders throughout. Replace them with values for
your own isolated test organization and channel.

## Register the source

1. Choose a source name, such as `teams-demo`, in the intended organization.
2. Derive its callback URL from the externally reachable Automation base URL:
   `https://automation.example.com/api/automation/v1/events/<ORG_ID>/teams-demo`.
   The `/api/automation` prefix is deployment configuration; use your actual
   external base path. Registration is not needed to compute this URL, and an
   unregistered source rejects deliveries.
3. Create the Teams Outgoing Webhook with that callback URL. Securely retain the
   security token Teams generates. Do not generate an OpenHands secret instead.
4. Register the source using an authenticated `POST /v1/webhooks` under your
   deployment's Automation base path. Put this JSON in a private local file,
   replace the placeholders, and send it as the request body:

```json
{
  "name": "Teams demo",
  "source": "teams-demo",
  "signature_scheme": "teams_hmac_sha256",
  "signature_header": "Authorization",
  "webhook_secret": "<TEAMS_ISSUED_BASE64_TOKEN>",
  "acknowledgement": "teams_message",
  "event_key_expr": "type",
  "event_id_expr": "join('/', [conversation.id, id])",
  "admission_expr": "channelData.tenant.id == '<TENANT_ID>' && channelData.team.id == '<TEAM_ID>' && channelData.channel.id == '<CHANNEL_ID>' && \"from\".aadObjectId == '<ALLOWED_REQUESTER_ID>'"
}
```

Teams signs the raw body using HMAC-SHA256 with the base64-decoded token and
sends `Authorization: HMAC <base64 digest>`. The token must be strict, nonempty
base64. A valid signature identifies the webhook, not authorization for every
team member to run an automation.

`admission_expr` must return the boolean `true` before Automation stores or
routes the delivery. Use explicit comparisons against stable tenant, team,
channel, and requester IDs. Per-automation filters can further restrict routing.
Automation checks syntax, function names, and argument counts when saving
`admission_expr` or `event_id_expr`. It checks argument types during delivery.

`event_id_expr` must return a nonempty string of at most 255 characters. Its value
must be unique within this organization/source: include conversation scope where
needed, as above, and keep the resulting value within that limit. Missing values,
invalid types and evaluation failures are rejected. Do not use message text as
the ID. `event_id_header` and `event_id_expr` cannot be configured together.

The Teams verifier and acknowledgement must be selected together, with both
expressions present. Invalid combinations return 422. Other signature schemes
keep the default acknowledgement and may independently use the optional body-ID
and admission expressions.

## Configure an event automation

Create or update an automation using the existing raw, prompt, plugin or draft
API. Its trigger can be:

```json
{
  "type": "event",
  "source": "teams-demo",
  "on": "message",
  "destination": "dispatch_run"
}
```

The existing tarball/plugin/prompt requirements still apply. A deterministic
script is sufficient for a verification demo; no LLM is required for webhook
delivery. The run executes as the automation's configured owner/service account.
Teams sender fields do not map to an OpenHands execution identity.

The source is an exact name. A list in `on` selects several event-key patterns,
not several sources. Management rejects continuation triggers on a deadline-bound
source, and rejects enabling that profile if an existing non-deleted continuation
automation uses it, including inactive automations. Draft validation reports the
same conflict. Ingestion independently rejects an incompatible active routing
configuration before storing an event or contacting the agent server.

## Acknowledgements and failures

An accepted delivery returns this fixed message activity after commit:

```json
{"type":"message","text":"Request received. Any matching automation will run separately."}
```

Duplicates and valid deliveries with no matching automation receive the same
acknowledgement. It confirms receipt, not task execution or success. It never
includes run IDs, organization details or reflected message text.

| Outcome | HTTP status | Stored event / new work |
| --- | --- | --- |
| Accepted | 200 | Event and matching pending runs committed |
| Duplicate within retention | 200 | No additional event or run |
| Valid activity, no match | 200 | Event retained, no run |
| Missing, invalid or tampered signature | 401 | None |
| Invalid JSON/activity/body ID | 400 | None |
| Source admission denied | 403 | None |
| Unknown or disabled source | 404 | None |
| Incompatible persisted continuation configuration | 409 | None |
| Storage failure | 5xx | No success acknowledgement; transaction rolled back |

Product telemetry is collected only after the event/run transaction commits.
For deadline-bound responses, FastAPI background tasks emit it after sending
the response, so telemetry network delays cannot hold the transaction or delay
the acknowledgement. This capture is best-effort, not a durable task queue.

Teams expects the acknowledgement within five seconds. No task execution or
outbound agent-server call occurs on this intake path, but the service and
database must still be available and responsive. Verify the deadline and header
preservation through your actual ingress; in-process route tests cannot prove
deployment timing or proxy behavior.

## Replay window and token replacement

Deduplication lasts while the event record is retained. The service setting
`integration_event_retention_days` defaults to 14 days. A captured, valid request
can be accepted again after that record is pruned. Reusing a retained ID is a
duplicate even if another signed payload has different content. The body is
signed, including its timestamp when present, but no timestamp freshness check
is enforced for this scheme.

To replace a Teams token, obtain the replacement from Teams, regenerating or
recreating the outgoing webhook as necessary, and send authenticated
`PATCH /v1/webhooks/<WEBHOOK_ID>` with `{"webhook_secret":"<NEW_TEAMS_TOKEN>"}`.
The change is atomic and immediately invalidates the old token; there is no
overlap window. Supplied tokens are write-only and not returned by the API.
The generated-secret `/rotate-secret` endpoint returns 409 for Teams sources.
Switching an existing source to the Teams scheme requires a new supplied token
and the complete Teams configuration in the same update.

Switching away from Teams retains the current secret until a separate rotation.
Disable the source, update it to the complete target protocol configuration,
call the generated-secret rotation endpoint and configure the target sender
with that new secret before re-enabling delivery.

## Compatibility and validation

Existing sources with the new fields unset keep their current signature,
header-based delivery IDs, routing and `EventResponse` behavior. The migration
adds three nullable columns; it does not convert existing sources to Teams.

`tests/test_teams_webhooks.py` exercises synthetic requests through the management
and event HTTP routes against SQLite and PostgreSQL. It covers admission,
signatures, duplicate handling, token replacement, continuation rejection and
transaction failure. This establishes the server contract; a live Teams check
and deployment-specific ingress/timing checks remain separate validation.

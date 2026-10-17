# Automation Service feature map

This directory is the maintained source for verifying the agent-facing
behavior of the OpenHands Automation Service: every REST route (`map routes`
lists them), the background actors that change state with no request
(scheduler, dispatcher, watchdog, git sync loop, stream sources), and the
contract between a dispatched run and the Agent Server it runs on. Read this
index before driving the service, then use the matching family file as the
recipe.

Maintenance baseline: `7cda26ab0f60158a2a6dc74964e9aaf84008e04a` (first pass, 2026-10-12).

The map has **257 sub-features**.

## Baseline preconditions

- `uv sync --dev` (or `make build`) has run in the repository root, and `uvx`
  is on `PATH` (it starts the Agent Server at the release the checkout pins).
- `control-automation` is on `PATH`:
  `export PATH="$PWD/.agents/skills/verify-automation/scripts:$PATH"`.
- A run of this checkout is live and exported:
  `export AUTOMATION_VERIFY_RUN=$(control-automation launch --new --print-run)`.
- `control-automation doctor` reports `ok: true`.
- Model-backed recipes: `$DEEPSEEK_API_KEY` is set and
  `control-automation llm preset deepseek` has run (deepseek-flash active on
  the Agent Server, which is where preset runs read their LLM from).
- Never drive a service that this verification run did not launch.

## Driving conventions

- Start every family from the baseline unless its `Preconditions:` say
  otherwise; run its bullets top to bottom in one shell.
- Recipes write service paths in the short form `/v1/...`; the CLI sends them
  to `/api/automation/v1/...`. Agent Server calls are written
  `control-automation api --agent-server METHOD PATH`.
- Capture per-run values into shell variables with `--field`; never paste ids
  from an earlier run. A bullet that reads another bullet's variables or files
  names it on a `Requires:` line, and a bullet that alone needs a machine
  capability names it on a `Needs:` line.
- Arrange with fixtures: `fixture automation --kind callback|echo|fail|fail-callback|sleep|kv`
  creates an automation from a stdlib-only tarball (entrypoint `bash run.sh`)
  that needs no network and no model. Use a preset (a real agent on DeepSeek)
  only where the agent's behavior is the thing under test.
- Await state changes with `run wait`, `api --until-ok` or
  `sink read --expect-min`, not `sleep`. The run launches with shrunk loop
  intervals (scheduler 5s, dispatcher 2s, watchdog 10s).
- Treat every command as literal. Keep quoted JSON and flags unchanged.
- Prefix names with `qa-` or `qa `; restore shared state (re-enable an
  automation, delete a webhook) after a mutation unless a later bullet needs it.
- Assert status codes, stored values, rows, files and the run's recorded
  outcome, never the model's wording.

## Proof and skip reporting

- A mutation is proven by a read-only second view: a GET, the runs list, a
  database row (`state sql`), a file in the run's workspace, a commit in the
  git remote.
- Save the exchange that proves each sub-feature with `--save <ID>/<name>` and
  cite it in `evidence add`.
- Report an unreachable path with the attempted command and the unmet
  precondition (`blocked`), never as a pass through a different entry point.

## Family entry contract

Each family file starts with an H1 title, one paragraph describing the
agent-visible behavior, a `Source:` line and a `Routes:` line (see
[mapping.md](../mapping.md#3-write-each-entry)). It then uses exactly four H2
sections in this order: `Sub-features`, `How to get to it (agent POV)`,
`Driving it with control-automation` (starting with `Preconditions:`), and
`Gotchas`.

How to write and prove new entries: [../mapping.md](../mapping.md). How to keep
them honest: [../maintenance.md](../maintenance.md).

## Families

| ID | Family | What it covers | Entry points | Needs | Sub-features |
|---|---|---|---|---|---|
| F01 | [Service status, version and API contract](F01-service-status.md) | liveness, readiness, SDK and service versions, OpenAPI document, router order, base path | `GET /health`, `/ready`, `/sdk-version`, `/server_info`, `/api/automation/openapi.json` | baseline | 7 |
| F02 | [Authentication and CORS](F02-auth-and-cors.md) | local key in header and bearer forms, session-key rotation, public routes, org scoping, permissions, auth cache, upstream errors, CORS | every `/v1` route | baseline | 18 |
| F03 | [Capabilities and preflight validation](F03-capabilities-and-validate.md) | capabilities by configuration and readiness, schedule, trigger, sample-event and profile validation | `GET /v1/capabilities`, `POST /v1/validate` | baseline | 17 |
| F04 | [Tarball uploads](F04-uploads.md) | upload, size and type limits, list, get, delete, use by an automation | `/v1/uploads` | baseline | 13 |
| F05 | [Automation CRUD and tarball download](F05-automation-crud.md) | create, list and filters, get, patch, delete, tarball download, template reuse | `/v1`, `/v1/{id}`, `/v1/{id}/tarball` | baseline | 21 |
| F06 | [Prompt and plugin presets](F06-presets.md) | prompt and plugin presets, generated tarball, repos, shared templates, continue-conversation rules | `/v1/preset/prompt`, `/v1/preset/plugin` | baseline | 11 |
| F07 | [Dispatch and run lifecycle](F07-dispatch-and-runs.md) | dispatch, run environment, phases, callbacks, cancel, runs list, a real preset run | `/v1/{id}/dispatch`, `/v1/{id}/runs`, `/v1/runs/{run_id}/...` | baseline; `llm`, `pypi` for the preset run | 16 |
| F08 | [Cron scheduling](F08-cron-scheduling.md) | scheduler fires, run environment, timezones, no catch-up, poll floor, disabled automations | the scheduler loop (no route) | baseline; `map run --timeout 400` | 7 |
| F09 | [Drafts](F09-drafts.md) | draft create from prompt, raw or plugin, validation, patch, test dispatch, delete | `/v1/drafts` | baseline | 19 |
| F10 | [Custom webhooks](F10-custom-webhooks.md) | webhook create, secrets and rotation, list, patch, disable, delete, signed delivery | `/v1/webhooks` | baseline | 12 |
| F11 | [Event ingestion and triggers](F11-event-triggers.md) | signature schemes, matching, filters, dedupe, built-in GitHub, Jira and Bitbucket sources, conversation coalescing, requested types | `/v1/events/{org_id}/{source}`, `/v1/events/{source}/requested-types` | `Launch:` webhook secret | 20 |
| F12 | [Per-automation KV store](F12-kv-store.md) | user and run-token auth, get, put, patch, delete, counters, lists, versions, batches, isolation, encryption | `/v1/kv` | baseline | 30 |
| F13 | [Git sync](F13-git-sync.md) | config, remote check, export and import, conflicts, deletes, encryption, errors, lease, loop interval | `/v1/git-sync/...` | `Launch:` git timeout | 29 |
| F14 | [Watchdog, timeouts and auto-disable](F14-watchdog-and-auto-disable.md) | watchdog verdicts, timeouts, late callbacks, auth-failure and consecutive-failure disable, workspace purge | the watchdog loop (no route) | baseline | 13 |
| F15 | [Telemetry consent](F15-telemetry-consent.md) | consent, capture gating, anonymous and aggregate events, persistence, cloud mode | `POST /v1/telemetry/consent` | `Launch:` telemetry sink | 9 |
| F16 | [The run's contract with the Agent Server](F16-agent-server-contract.md) | bash command and output, uploads cleanup, conversation tags, model profiles, agent-server failures | Agent Server `/api/...` | baseline; `llm`, `pypi` | 8 |
| F17 | [Stream sources (Slack Socket Mode)](F17-stream-sources.md) | stream supervisor, config parsing, retries, kill switch, Socket Mode delivery | the stream supervisor (no route) | baseline; a real Slack app for two blocked bullets | 7 |

## Not mapped

Routes deliberately left without an owner, with the reason:

- `GET /api/automation/docs`, `GET /api/automation/redoc`,
  `GET /docs/oauth2-redirect`: FastAPI's generated documentation pages, not
  part of the agent-facing contract (`/api/automation/openapi.json` is mapped
  in F01).

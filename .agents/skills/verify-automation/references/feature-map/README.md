# Automation Service feature map

This directory is the maintained source for verifying the agent-facing
behavior of the OpenHands Automation Service: every REST route (`map routes`
lists them), the background actors that change state with no request
(scheduler, dispatcher, watchdog, git sync loop, stream sources), and the
contract between a dispatched run and the Agent Server it runs on. Read this
index before driving the service, then use the matching family file as the
recipe.

Maintenance baseline: `7cda26ab0f60158a2a6dc74964e9aaf84008e04a` (first pass, 2026-10-12).

The map has **0 sub-features**.

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

## Not mapped

Routes deliberately left without an owner, with the reason:

- `GET /api/automation/docs`, `GET /api/automation/redoc`,
  `GET /docs/oauth2-redirect`: FastAPI's generated documentation pages, not
  part of the agent-facing contract (`/api/automation/openapi.json` is mapped
  in F01).

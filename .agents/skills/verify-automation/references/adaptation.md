# Where this workflow comes from

This is an independently written adaptation of ideas from poteto's
MIT-licensed pstack ([backnotprop/pstack](https://github.com/backnotprop/pstack),
a harness-neutral mirror of
[cursor/plugins/pstack](https://github.com/cursor/plugins/tree/main/pstack)) and
the [cursor-team-kit](https://github.com/cursor/plugins/tree/main/cursor-team-kit)
control skills, by way of two sibling skills: `verify-openhands` in
[`OpenHands/OpenHands`](https://github.com/OpenHands/OpenHands/tree/main/.agents/skills/verify-openhands),
which applies the method to the Agent Canvas UI, and `verify-agent-server`
([`OpenHands/software-agent-sdk#5621`](https://github.com/OpenHands/software-agent-sdk/pull/5621)),
which applies it to the Agent Server's REST and WebSocket API. The map
tooling in `control_automation.py` (`map check`, `coverage`, `run`, `owners`,
`diff`, the evidence ledger, `known bug` and `blocked` bullets) is ported from
`control_agent_server.py`, so both maps keep one format.

- [create-verification-skill](https://github.com/cursor/plugins/blob/main/pstack/skills/create-verification-skill/SKILL.md):
  interview the repository, then generate launch / doctor / drive / evidence /
  cleanup and a feature map, and prove one recipe end to end before handing
  it over.
- [The feature-map example](https://github.com/cursor/plugins/tree/main/pstack/skills/create-verification-skill/references/feature-map-example):
  one file per feature with four sections, stable IDs, and every step written
  against a `control-<app>` CLI so it is a literal, rerunnable command.
- [maintain-verification-skill](https://github.com/cursor/plugins/blob/main/pstack/skills/maintain-verification-skill/SKILL.md):
  index hygiene, a parallel source wave, one serial live pass, and triage into
  map drift, harness gaps and product bugs.
- [Build the Lever](https://github.com/cursor/plugins/blob/main/pstack/skills/principle-build-the-lever/SKILL.md)
  and [control-cli](https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/control-cli/SKILL.md):
  build the tool that drives and proves the work; one action per step,
  deterministic waits over sleeps, clean up what you started.
- [Benny's control-adapter contract and verify-existing-fix](https://github.com/backnotprop/pstack/tree/main/automations/benny/skills/reproduce-and-fix-issues/references):
  report capabilities and which map sections are drivable or blocked before
  starting, translate the environment before declaring a block, and confirm a
  fix only when the baseline reproduces twice and the patched build resolves
  it twice.
- [cli-for-agents](https://github.com/cursor/plugins/blob/main/cli-for-agent/skills/cli-for-agents/SKILL.md):
  non-interactive flags, layered `--help` with examples, actionable errors,
  idempotent commands, machine-readable output.

## Automation Service decisions

The "user" of this service is an agent, or the program that carries one: an
agent that creates and dispatches automations through the API, the preset
script a run executes, the SDK's completion callback, a webhook sender. So the
surface under test is the REST API plus what a dispatched run receives and
calls back, not a screen.

- **A CLI over raw HTTP, not a client library.** No client wraps every route;
  Agent Canvas and the `automation` skill in `OpenHands/extensions` call a
  subset. Driving the wire keeps every route, status code and error body
  reachable on the day it lands. `exec` still runs any client program
  against the same isolated run.
- **The stack, not the service alone.** A run is executed by a real Agent
  Server (the release the checkout's `openhands-sdk` pin names, started with
  `uvx`), so dispatch, bash execution, the watchdog's verification, the
  preset's agent, its conversation and its callback are all real.
  `api --agent-server` and `conversation events` read what the run left
  there. The Agent Server's own API belongs to its own map; this one maps
  only the contract the service depends on (F16).
- **Fixtures instead of mocks.** Stdlib-only tarballs (`fixture tarball`,
  `fixture automation`) call back, fail, sleep or use the KV store from
  inside a real run, with no model and no network, so run-lifecycle, watchdog
  and auto-disable recipes are fast and deterministic. A real agent on
  DeepSeek V4.1 Flash runs only where the agent is the thing under test.
- **Signed deliveries as a verb.** `event send` signs webhook deliveries with
  each scheme the service verifies (HMAC-SHA256 hex, Standard Webhooks, Slack
  v0) and can tamper, age or drop them, so ingestion is driven exactly as a
  provider would.
- **The route table is the inventory.** `map routes` imports the checkout's
  app. Each family declares the routes it owns on a `Routes:` line; `map
  coverage` fails until every route has exactly one owner, and `map check`
  fails when a recipe or a `Routes:` line names a route the app no longer
  serves. Background actors (scheduler, watchdog, git sync loop, streams) get
  `Routes: none` families observed through the runs API, the database and
  the log.
- **Recipes are parsed, not just read.** `map check` runs every
  `control-automation ...` command in the map through the real argument
  parser, so a renamed verb or flag breaks the check instead of the next agent.
- **Isolation by configuration.** Every run gets a private `HOME`, SQLite
  database, file store, workspace base, git workdir, Agent Server state and
  tmux dir, its own local API key, KV secret and ports, and inherits none of
  the caller's `AUTOMATION_*`, `OH_*` or LLM variables.
- **Fast loops.** Runs launch with the scheduler, dispatcher and watchdog
  intervals shrunk to seconds (`--production-intervals` keeps the defaults),
  so state changes show up within a recipe's wait.
- Not imported: the browser daemon and screenshots of the UI skill (there is
  no UI here), WebSocket verbs (the service has none), Cursor plugin
  configuration, model routing, and autonomous shipping or merge authority.

These are contributor workflows for this repository under `.agents/skills`
([AgentSkills format](https://docs.openhands.dev/sdk/guides/skill)). They
complement the unit and integration tests under `tests/`, and do not replace
them.

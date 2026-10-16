---
name: verify-automation
description: >
  This skill should be used to "verify the Automation Service APIs", "test the
  automation REST API like an agent would", "drive automations end to end",
  "check an automation change against a real service", "create or update the
  automation feature map", or "run the weekly automation API audit". Ships
  control-automation (launch, doctor, api, llm, run wait, fixtures, signed
  events, KV tokens, state, assert, evidence, map) and the maintained map of every
  agent-facing feature.
triggers:
- /verify-automation
- automation feature map
---

# Verify the Automation Service through its real API

Prove what an API consumer observes, not that a handler returned or CI passed.
The consumers are agents and the programs that carry them: an agent that
creates and dispatches automations (the `automation` skill in
`OpenHands/extensions`, Agent Canvas), the preset script a run executes, the
SDK's completion callback, and the providers that deliver webhooks. Three
parts work together:

1. **`control-automation`** ([scripts/](scripts/)) is the lever. It launches
   this checkout's service in local mode next to a real Agent Server (the
   release the checkout's `openhands-sdk` pin names), each with a private
   `HOME`, database, file store, workspace, keys and ports, and turns every
   API step into a command you can rerun: REST calls, signed webhook
   deliveries, run waits, fixtures, database reads, and the conversation a
   run's agent left on the Agent Server.
2. **The feature map** ([references/feature-map/](references/feature-map/README.md))
   lists every agent-facing behavior with stable IDs, the routes each family
   owns, entry points, exact `control-automation` recipes, observable results
   and gotchas. Background actors (scheduler, dispatcher, watchdog, git sync,
   stream sources) have families too.
3. **Evidence**: saved request/response exchanges, run records, conversation
   events and program transcripts, plus a pass/fail/blocked/not-run ledger
   that survives cleanup.

Neither this skill nor the map authorizes external writes, paid models beyond
the budget you were given, or product fixes the user did not ask for.

## The CLI comes first

Put it on `PATH` and read its help before anything else:

```sh
export PATH="$PWD/.agents/skills/verify-automation/scripts:$PATH"
control-automation --help              # then: control-automation <command> --help
```

Requirements: `uv sync --dev` (or `make build`) in the repository root, `uvx`
(it starts the Agent Server), `git` for git sync, and a DeepSeek key for
model-backed recipes. The first launch downloads the Agent Server release
into uv's cache, which takes a few minutes; later launches take about 15
seconds. Every command prints one JSON object; exit 0 ok, 1 action failed,
2 usage, 3 environment.

If an agent-facing path cannot be driven with the CLI, that is a **harness
gap**: extend `scripts/control_automation.py` (document the verb in its
`--help` with examples and in this file), prove it live, then write the
recipe. Never work around a gap with an untracked one-off script the next
agent cannot rerun. `api` reaches every route, so most gaps are about
waiting, fixtures or observing side effects, not about calling an endpoint.

## Launch → doctor → drive → evidence → cleanup

```sh
export AUTOMATION_VERIFY_RUN=$(control-automation launch --new --print-run)
control-automation doctor                     # read-only; must be ok before driving
control-automation llm preset deepseek        # deepseek-flash (active) + deepseek-pro on the Agent Server
AID=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa hello","prompt":"Create a file named hello.txt in the current directory containing exactly: hi. Then call the finish tool.","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --field id)
RID=$(control-automation api POST "/v1/$AID/dispatch" --expect 201 --field id)
control-automation run wait "$AID" --run-id "$RID" --status COMPLETED --timeout 900 --check conversation_id exists --save F07.preset-run/run
control-automation state cat "workspaces/automation-runs/$RID/hello.txt" --contains hi
CID=$(control-automation run wait "$AID" --run-id "$RID" --field conversation_id)
control-automation conversation events "$CID" --expect-tool finish
control-automation evidence add --feature F07.preset-run --result pass --entry "POST /v1/{id}/dispatch" \
  --expected "COMPLETED with a conversation" --actual "COMPLETED" --artifact evidence/F07.preset-run/run.json
control-automation stop                       # stops only this run; evidence stays
```

- **Which run.** Commands use `--run DIR` (anywhere on the line), else
  `$AUTOMATION_VERIFY_RUN`, else the only live run. With several live runs
  they refuse to guess, so export `AUTOMATION_VERIFY_RUN` in every shell when
  other agents share the machine. Never `pkill -f` or `killall` by pattern;
  `control-automation stop` ends only your run's process groups.
- **Launch** starts the Agent Server (`uvx --from openhands-agent-server==<pin>`,
  or `--agent-server-path` for an SDK checkout, or `--agent-server-url` to
  attach) and then the service (`python -m uvicorn openhands.automation.app:app`
  from the checkout's `.venv`) in local mode: `AUTOMATION_AGENT_SERVER_URL`,
  a local API key (`AUTOMATION_LOCAL_API_KEY`), SQLite in the run directory,
  the local file store, `AUTOMATION_BASE_URL` set to the service's own URL so
  run callbacks reach it, a KV secret, and scheduler/dispatcher/watchdog
  intervals of 5/2/10 seconds (`--production-intervals` keeps 60/10/60). The
  Agent Server gets what Agent Canvas exports to it (`AGENT_SERVER_URL`,
  `OPENHANDS_AUTOMATION_API_KEY`). The caller's `AUTOMATION_*`, `OH_*` and LLM
  variables are not inherited; pass what a recipe needs with `--env K=V`
  (service), `--as-env K=V` (Agent Server) or `--pass-env NAME`. Other
  variants: `--env AUTOMATION_WEBHOOK_SECRET=...` (built-in webhook sources),
  `--telemetry-sink` (PostHog pointed at a recording sink), `--checkout PATH`
  (another worktree, for a baseline).
- **Doctor** checks both process groups and that this run owns their ports,
  the probes with and without the base path, `/server_info` and
  `/sdk-version`, that a missing or wrong key is rejected (401) and the run's
  key accepted, that a rotated-out key is rejected, the Agent Server's
  readiness, key and active LLM profile, the database file, and tracebacks in
  both logs.
- **Drive** with `api METHOD PATH` (any route; `/v1/...` is short for
  `/api/automation/v1/...`; `--agent-server` before METHOD calls the Agent
  Server instead; `--auth key|bearer|none|bad|bearer-bad|previous` for auth
  cases; `--expect`, `--check FIELD OP VALUE`, `--check-header`; `--field` to
  chain values into shell variables; `--data-file` with `--content-type` for
  raw uploads; `--until-ok` to poll; `--save FEATURE/NAME` for evidence),
  `run wait` (follow a run through PENDING, RUNNING and a terminal status,
  with its phase history), `event send` and `event requested-types` (signed
  deliveries: `hmac_sha256_hex`, `standard_webhooks`, `slack_v0`, built-in
  sources with `--builtin`; `--tamper`, `--age`, `--unsigned`,
  `--delivery-id` for the refusal and dedupe cases), `kv token` (the JWT a run
  receives), `state sql|ls|cat` (read-only views of the database, the run's
  workspaces and fixtures), `conversation events` (what a run's agent did),
  `sink read`, `logs --grep`, `exec -- <program>` (a client program with
  `AUTOMATION_URL`, `AUTOMATION_API_URL`, `AUTOMATION_API_KEY` and
  `AGENT_SERVER_URL` injected), and `assert --value|--json --check ...` (a
  value the recipe computed, checked like `api --check`, so a `# bug:` line
  can name the value the bug produces).
- **Arrange, don't fake.** `fixture tarball` and `fixture automation` build
  stdlib-only automations (entrypoint `bash run.sh`) that really run on the
  Agent Server: `callback` reports a phase, dumps its environment to
  `qa-env.json` in its workspace and completes through the callback; `echo`
  exits 0 without a callback; `fail` exits 3; `fail-callback` reports FAILED;
  `sleep` never finishes; `kv` uses `AUTOMATION_KV_TOKEN` from inside the run.
  `fixture git-remote` makes a bare repository for git sync, and
  `fixture http-sink` records requests or serves a tarball for external URLs.
  Fixtures, `llm` and setup calls never count as proof of the feature that
  consumes them. Never patch the service or mock a response to make a live
  check pass. A recipe-local process that later bullets use runs under
  `helper start --name NAME -- CMD`, not `CMD &`.
- **State control** reaches states a happy path never shows, without mocks:
  `restart --service automation --env K=V` (thresholds, timeouts, secrets),
  `restart --reset-env`, `restart --rotate-key` (stale key), `restart --hard`
  (SIGKILL), `service stop|start agent-server` (an Agent Server outage while
  runs are in flight).
- **Evidence** goes under `<run>/evidence/<feature-id>/` and the append-only
  ledger `<run>/evidence/ledger.jsonl`. Nothing is overwritten: a repeated
  name is saved as `<name>-2.json`, so cite the path the command prints;
  `evidence report` renders [the report contract](references/report.md). The
  CLI redacts the run's keys, the KV secret, `$DEEPSEEK_API_KEY` and
  Fernet-encrypted values from output and evidence; only
  `--unsafe-unredacted` with `--field` or `--print-header` returns a raw value
  (for a webhook secret a recipe signs with), and `state sql --raw` shows
  stored values to prove they are encrypted. Keys, logs and the database stay
  in `<run>/private/`. Evidence is not automatically public: read it before
  publishing it.
- **Cleanup** with `control-automation stop` (add `--purge-private` to delete
  keys, database, workspaces and fixtures once the evidence is checked). It
  signals only the process groups this run started and verifies the ports
  closed.

## LLM budget

Preset runs read their model from the Agent Server's active profile (or the
automation's `model`, a profile name). Use `deepseek-flash` (DeepSeek V4.1
Flash) for everything that needs a model; use `deepseek-pro` only for checks
that need a second profile. Keep prompts tiny, deterministic and confined to
the run's workspace ("Create a file named hello.txt in the current directory
containing exactly: hi. Then call the finish tool."), and assert on the run
record, files and the conversation's tool calls, never on the model's
wording. A preset run also installs `openhands-sdk` from PyPI in its
`setup.sh`, so it needs network access to PyPI. Without a key, run every
credential-free recipe (most of the map uses fixtures and needs no model) and
record model-dependent ones as `blocked`; never substitute a mock and call it
a pass.

## Choose the job

- **Verify a change or a PR**: map the changed paths to feature IDs with
  `control-automation map owners --changed origin/main...HEAD`, drive every
  entry point those features list, and report with
  [the report contract](references/report.md). A behavior change needs a
  regression test under `tests/` as well; the recipe is the live proof, not a
  substitute for that test.
- **Verify an existing fix** (a PR or commit that claims to fix a `known bug`
  bullet): reproduce on the baseline twice and the fix twice, with the same
  flags and fixtures:
  `control-automation map run --file Fnn --only Fnn.x --fresh --repeat 2 --checkout <base worktree>`
  must report the bullet `xfail` in both attempts, and the same command
  without `--checkout` `xpass` in both. `xfail` means the marked command
  failed with the failure its `# bug: PREDICATE` documents (see
  [the known-bug contract](references/mapping.md)); any other failure on the
  baseline is a `fail`, so the proof cannot rest on an unrelated error. Then
  drop the marker in the same PR.
- **Create or extend the map**: follow [references/mapping.md](references/mapping.md).
  `control-automation map coverage` measures which routes no family owns or
  drives.
- **Re-prove the map**: `control-automation map run --file Fnn --fresh --record`
  replays a family on its own fresh run (with the family's `Launch:` flags)
  and writes ledger rows; `control-automation map run --all --fresh --record`
  replays the whole map. Only failing bullets need judgment; `known bug`
  bullets report as expected failures until the bug is fixed.
- **Maintain the map (periodic or weekly)**: follow
  [references/maintenance.md](references/maintenance.md): a frozen
  BASE/TARGET, a route diff, a PR-intent ledger, a source wave, one live pass
  over every family, and at most one PR of proven corrections.

## Triage what you find

- **Map drift**: the map describes something the service no longer does by
  design. Fix the entry with current source and live evidence.
- **Harness gap**: the service works but the CLI cannot drive or observe it.
  Fix the CLI, re-drive.
- **Product bug**: the service is broken. Keep the evidence, search existing
  issues, and report it separately: scheduling, dispatch, webhooks, KV and git
  sync here (`OpenHands/automation`); the Agent Server, SDK and preset agent
  behavior in `OpenHands/software-agent-sdk`; Agent Canvas in
  `OpenHands/OpenHands`. Never rewrite an expected result to bless broken
  behavior.
- **Blocked**: name the missing prerequisite (a Slack app, a cloud
  deployment, PostgreSQL, a key) and the route you attempted. Unreachable is
  never a pass. `control-automation capabilities` says what this machine can
  drive and which families' `Needs:` are unmet.

## How this fits with the tests

The unit and integration tests under `tests/` prove handlers, models and
loops in isolation, with an in-memory database and mocked Agent Server calls.
This skill proves the assembled stack as agents use it: one real service
process with its background loops, a real Agent Server executing runs, real
callbacks, real signatures, a real git remote and a real model. It does not
replace the tests. `tests/test_verify_automation_skill.py` guards the CLI and
the committed map's structure.

See [references/adaptation.md](references/adaptation.md) for where these ideas
come from and what was deliberately left out.

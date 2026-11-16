# Local review: avoid duplicate SDK initialization in preset setup

Issue: [OpenHands/automation #668](https://github.com/OpenHands/automation/issues/668), labelled `enhancement` and `ready-for-dev`.

Proposed title: `perf(presets): avoid redundant SDK imports during setup`

Branch: `perf/preset-metadata-check`, based on `506642221fa5255464024fc2c29dd8d5d883e31a` (automation 1.30.2). Local review only: no branch push, issue comment, or pull request has been posted.

## Change

The prompt, plugin and plugin-spec setup scripts install three SDK distributions, then import all three modules to verify the install. Each entrypoint immediately imports those modules again in another Python process. That initializes the SDK twice and adds unnecessary startup time.

All three setup scripts now query `importlib.metadata.version()` for `openhands-sdk`, `openhands-tools` and `openhands-workspace`. This standard-library lookup reads installed distribution metadata without executing the modules. A missing distribution still causes a nonzero setup exit with the existing error message. The entrypoint still initializes the actual SDK and reports any import-time problem there; metadata presence alone is not a guarantee that module initialization will succeed.

The production change is limited to these three verification blocks. Package versions, installer arguments, Python selection, venv replacement behavior (#665), callbacks, progress phases, tarball/entrypoint protocols and execution behavior are unchanged. No dependency, API, schema or SDK release is needed. The same setup files are shipped to Local and Cloud runs; current runner refresh also supplies them to existing preset automations.

## Tests

The existing `tests/test_preset_router.py` now has 12 parameterized runtime cases: all three presets with all distributions present, then each of the three distributions absent. Each test executes the real shell script with an isolated Python venv and real distribution metadata. The installer/API response are stubbed for this unit test; package modules deliberately raise if imported, proving the check does not initialize them.

- [Before implementation](regression-before.log): all 12 new cases fail because the old check imports the SDK.
- [After implementation](regression-after.log): all 12 pass.
- [Full repository pre-commit checks](pre-commit.log): YAML formatting, Ruff formatting/lint, pycodestyle and Pyright all pass.
- [Full repository test suite](full-tests.log): **2,829 passed, 7 skipped**, 126 existing warnings, no failures; 403.67 seconds. PostgreSQL-backed tests ran with Docker. The excerpt names the skipped cases.

Development environment: macOS arm64; Python 3.12.8; locked dependencies installed with `uv sync --frozen --group dev`. Docker Desktop supplies the PostgreSQL/test-storage fixtures. Its credential helper initially blocked image pulls; the successful retry uses an isolated empty `DOCKER_CONFIG` and the local Docker socket for anonymous public-image access, without changing the user's Docker configuration. `uv.lock` and `pyproject.toml` are unchanged.

## Measured before and after

[Raw results](benchmark.json) · [reproduction driver](benchmark.py)

| Measurement | Original | Fixed |
|---|---:|---:|
| Complete setup, median of three fresh venvs with a warm uv cache | 8.707 s | 0.556 s |
| Standalone verification, median of three subprocesses | 2.041 s | 0.033 s |

The first standalone original import in the fixed venv took 8.202 s before bytecode caches existed; later original-import samples can reuse bytecode. Complete-setup measurements each use a separate fresh venv, alternate execution order, and exclude the cache-warmup run. Setup includes version discovery, venv creation and actual package installation. These are local measurements, not a claim to reproduce the issue's 43-second Linux result or a universal automation-runtime speedup.

The setup scripts selected an available CPython 3.13.9 interpreter, within their unchanged supported range. All three installed packages were 1.60.2, fetched from the real automation service's `/sdk-version` endpoint. [Other preset smoke results](other-presets.json) also confirm successful real plugin and plugin-spec setup (0.627 s and 0.538 s respectively). The logs for each sample are included here.

Reproduce with a fresh runtime directory and a live automation URL:

```sh
.venv/bin/python .pr/preset-metadata-check/benchmark.py \
  --api-url http://127.0.0.1:19400/api/automation \
  --runtime /private/tmp/oh668-new-measurement
```

## Real entrypoint smoke check

The isolated stack used the automation checkout and a real Agent Server 1.60.2. [Health check](stack-health.json). The prompt entrypoint runs from the final measured venv with the unchanged production `sdk_main.py`, a temporary working directory, and a dummy LLM profile whose base URL points at a non-listening localhost port.

[Probe driver](probe_entrypoint.py) · [entrypoint log](entrypoint.log) · [probe result](entrypoint.json) · [server readback](server-conversation.json)

The actual SDK imports, workspace initialization, skill loading, settings reads, agent construction and remote conversation creation succeeded. The Agent Server confirms that the resulting conversation exists and is idle. A Python trace hook stops immediately before `conversation.send_message()` and `conversation.run()` to avoid provider calls. The existing log line saying "sending prompt" precedes that hook; no prompt was sent. No model was invoked, no paid key was supplied, and this is an initialization smoke check rather than a completed model-backed automation run. Cloud orchestration was not exercised; the changed package-metadata check itself is the same there.

To reproduce after configuring an isolated dummy local profile, use the measured runtime's Python:

```sh
/private/tmp/oh668-runtime/after-3/.venv/bin/python \
  .pr/preset-metadata-check/probe_entrypoint.py \
  --work-dir /private/tmp/oh668-new-entrypoint \
  --session-key-file <owned-run>/private/session-key \
  --agent-server-url http://127.0.0.1:19401
```

## Human review before submission

Read the three small shell diffs and the runtime test, then inspect the measured samples. This is prepared for CS146S contribution 2; the course requires you to understand and stand behind the submitted code. No PR will be opened before your review. The HUMAN section, if required by the submission template, must be written by you.

The isolated verification stack was stopped after the smoke check; its owned ports closed and its private runtime/profile state was purged. Docker Desktop was started for the test suite; unrelated existing containers were left untouched.

## Quick terminal demo for human review

These commands need no running service or model key. The demo uses the existing
benchmark venv; it only reads package metadata and imports installed modules.

```sh
cd ~/Documents/cs146s/automation
uv run --frozen python .pr/preset-metadata-check/demo.py
uv run --frozen pytest tests/test_preset_router.py -k verifies_installed_distributions -v
```

The first command compares the actual old and new verification code three times
each and prints their median duration. It measures the verification step only,
excluding installation and venv creation. The second command should show 12 passing
cases: all packages present and each package missing, across all three presets.

---
name: custom-codereview-guide
description: Repository-specific review guidance for OpenHands/automation.
triggers:
  - /codereview
---

# OpenHands/automation review guide

Apply this guide with the general code-review skill and `AGENTS.md`. Use APPROVE
when the current head has no material correctness, security, compatibility, or
acceptance-criterion defect. Use COMMENT for a material finding. Do not leave
inline comments for optional refactors, style, or speculative improvements.

The repository's protected branch requires an approval. If the verdict is worth
merging, the risk is low, and there are no unresolved material findings, submit
APPROVE rather than a COMMENT that says the PR is ready.

## Repository ownership

This repository owns automation definitions, scheduling, webhook and stream
intake, run history, dispatch, and sandbox lifecycle orchestration. Agent and tool
behavior, conversations, workspaces, events, server endpoints, and the browser
client belong in `OpenHands/software-agent-sdk`; Canvas UI belongs in
`OpenHands/OpenHands`; reusable extension content belongs in
`OpenHands/extensions`.

## Blocking checkpoints

### Run state and lifecycle

Apply this checkpoint to scheduling, dispatch, callbacks, watchdogs, retries, and
cleanup. Trace every affected run state from creation to one terminal result.
Verify that:

- two workers, a callback and watchdog, or a retry cannot both claim or complete
  the same run;
- a terminal transition is idempotent and cannot move backward;
- failure and cancellation release leases and clean up sandboxes and temporary
  artifacts;
- persisted status matches the real sandbox or process outcome; and
- fire-and-forget work has an owner that observes and records failure.

Identify the concrete interleaving or leaked resource. Do not request a new lock
or abstraction without one.

### Scheduling, overlap, and capacity

For cron, timeout, concurrency, or retry changes, compare the maximum run duration
with the minimum schedule interval and determine whether one automation can
overlap itself. Trace the behavior at the per-user sandbox limit; automation work
must not silently pause or evict the user's interactive sandbox. A skip or
backpressure path counts only if its producer can actually return the status it
handles. Verify that stale-run detection uses observable liveness where
available, rather than waiting only for the maximum wall-clock timeout.

### Authorization, secrets, and untrusted inputs

Verify organization and automation ownership at every read, mutation, callback,
upload, and dispatch boundary. Forward only the credentials required by the
sandboxed workload, and do not log or persist plaintext tokens. Treat webhook
payloads, tarballs, repository paths, entrypoints, and git-synced files as
untrusted: validate paths before extraction or deletion and authorize the actor
whose identity will execute imported automation content.

### Database and migration parity

Behavior must work with PostgreSQL and local SQLite. Use generic SQLAlchemy types
unless a guarded PostgreSQL-only feature is required. Review row-locking code
with the SQLite fallback and concurrent-replica behavior. A PR should contain one
coherent migration for its schema change, with one Alembic head and a complete
upgrade path from the released schema.

### API and extension contracts

When request schemas, preset generation, environment variables, callback
payloads, or catalog bundles change, trace the contract through its producer and
consumer. Preserve compatibility or coordinate the required SDK, Canvas, or
extensions release. Do not approve examples or generated tarballs that rely on
environment variables, package versions, or endpoints the production dispatcher
does not provide.

## Evidence

Lifecycle and integration fixes need a test that exercises the relevant state
transitions and a real-path command or run showing the final run and sandbox
state. Mocks that only assert calls do not prove race handling or cleanup. Apply
the smallest relevant matrix: PostgreSQL/SQLite, local/cloud, or callback/watchdog
only when those paths differ.

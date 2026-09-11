# Draft event automation REST QA

Date: 2026-09-11
Branch: `openhands/draft-lifecycle`
PR: #439

## Result

PASS

## How this QA was run

This QA used the actual automation backend process and REST API calls:

- Started backend with `uv run uvicorn openhands.automation.app:app --host 127.0.0.1 --port 8123`.
- Configured local mode with `AUTOMATION_AGENT_SERVER_URL=$RUNTIME_URL`, `AUTOMATION_AGENT_SERVER_API_KEY=$SESSION_API_KEY`, and `AUTOMATION_LOCAL_API_KEY=qa-local-key`.
- Used a temporary SQLite database and local file storage.
- Used `curl` against `http://127.0.0.1:8123/api/automation/v1` for the API flow.
- Used a raw custom tarball whose `main.py` wrote the dispatched event payload to the local workspace and posted the normal `/runs/{run_id}/complete` callback.
- No Cloud sandbox was created. The dispatcher used local execution and recorded `sandbox_id: null` on completed runs.

## Flow verified

1. Created custom event source `qa-rest-events`.
2. Created an incomplete event-based raw draft.
   - Draft ID: `c2691996-ed05-46ac-845e-1519e1d12d12`
   - `dispatchable`: `false`
   - `materialized_automation_id`: `null`
   - Validation error fields: `tarball_path`, `entrypoint`
3. Uploaded a custom script tarball through `POST /uploads`.
   - Tarball path: `oh-internal://uploads/1a6a898a-6f47-4eba-9770-1c191de7e1b1`
4. Patched the draft complete.
   - `dispatchable`: `true`
5. Dispatched the first draft test run by `POST /drafts/{draft_id}/dispatch` with synthetic event payload and no webhook signature headers.
   - Materialized automation ID: `a762c275-7b06-4818-9151-5c7ed5ce474d`
   - Run ID: `eafb4c97-f36a-4f8b-aff2-d168505c9801`
   - Initial status: `PENDING`
6. Confirmed the first run executed through local backend dispatch and completed by callback.
   - Final status: `COMPLETED`
   - `bash_command_id`: `41e8ea4c2b0e499dbf6a7c689eb642f7`
   - `sandbox_id`: `null`
7. Re-modified the draft.
   - New name: `QA REST event draft v2`
   - New timeout: `121`
8. Dispatched the second draft test run with a different synthetic event payload.
   - Reused automation ID: `a762c275-7b06-4818-9151-5c7ed5ce474d`
   - Run ID: `3a5dfa19-52a8-40bf-a7b0-4f1e40e8ac9d`
   - Initial status: `PENDING`
9. Confirmed the second run executed through local backend dispatch and completed by callback.
   - Final status: `COMPLETED`
   - `bash_command_id`: `108d4ad6dfda4cce90980fae4a51b6dc`
   - `sandbox_id`: `null`
10. Activated the materialized automation through `PATCH /{automation_id}`.
    - Final `state`: `ACTIVE`
    - Final `enabled`: `true`
    - Final name: `QA REST event draft v2`
    - Final timeout: `121`

Raw JSON evidence is in `.pr/qa-rest-draft-event-state.json`.

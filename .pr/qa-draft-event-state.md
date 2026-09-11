# Draft event automation QA

Date: 2026-09-11
Branch: `openhands/draft-lifecycle`
PR: #439

## Result

PASS

## Scope

Exercised the requested draft state flow through the FastAPI app using authenticated API requests, a SQLite test database, and an in-memory file store.

Because this PR QA ran in the development container without a live OpenHands sandbox backend, dispatcher selection was exercised and the async sandbox execution task was replaced with a QA completion stub. This confirms the run is accepted by the dispatcher and reaches `COMPLETED` in the test harness without making external sandbox/network calls.

## Flow verified

1. Configured custom event source `qa-draft-events`.
2. Created an incomplete event-based prompt draft.
   - Draft ID: `40718596-56a1-48e7-843e-00263b53f0bd`
   - `dispatchable`: `false`
   - `materialized_automation_id`: `null`
   - Validation error fields: `prompt`
3. Patched the draft to make it complete.
   - `dispatchable`: `true`
4. Dispatched the first test run with a synthetic event payload, without webhook signature headers.
   - Materialized automation ID: `e74eac21-fa00-4ffe-bd1a-0e7ff7be6796`
   - Run ID: `3567655c-0c97-467c-ab78-a99b109b3f63`
   - Initial run status from draft dispatch: `PENDING`
   - Materialized automation state: `DRAFT`
   - Materialized automation enabled: `false`
   - Stored event payload:
     ```json
     {"type":"qa.event.created","revision":1,"message":"first synthetic test"}
     ```
5. Confirmed the first test run was selected by the dispatcher and completed by the QA stub.
   - Final run status: `COMPLETED`
6. Re-modified the draft prompt and dispatched a second test run with a different synthetic payload.
   - Reused materialized automation ID: `e74eac21-fa00-4ffe-bd1a-0e7ff7be6796`
   - Run ID: `8e4d8fdc-df7c-42cc-84c3-026354867d15`
   - Initial run status from draft dispatch: `PENDING`
   - Stored event payload:
     ```json
     {"type":"qa.event.created","revision":2,"message":"second synthetic test"}
     ```
   - Materialized prompt after re-test: `QA rev2: acknowledge the updated synthetic event payload.`
7. Confirmed the second test run was selected by the dispatcher and completed by the QA stub.
   - Final run status: `COMPLETED`
8. Activated the materialized automation through the normal automation API.
   - Automation ID: `e74eac21-fa00-4ffe-bd1a-0e7ff7be6796`
   - Final `state`: `ACTIVE`
   - Final `enabled`: `true`

## Notes

- Draft dispatch accepted synthetic `event_payload` bodies from authenticated requests.
- No webhook delivery endpoint was called, and no webhook signature headers were supplied for the test dispatches.
- A custom webhook source was still configured because event-draft validation currently requires the event source to exist before a draft is dispatchable.
- Raw JSON evidence is in `.pr/qa-draft-event-state.json`.

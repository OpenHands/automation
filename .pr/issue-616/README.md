# #616 evidence

`demo_616.py` is a simulation, copied into `tests/` to run (it needs the test Postgres fixtures and an agent-server venv at `~/work/as-venv`). A real agent-server 1.62.0 runs one conversation against a mock LLM (one `task_tracker` call, then a final reply) and posts its events to a fake app-server webhook with the default `WebhookSpec` (batches of 5, partial batch after 30 s idle). The real `complete_run` route and the real watchdog sweep (`cleanup_due_sandboxes`, every 5 s) tear the sandbox down through a fake runtime API that kills the agent-server on DELETE and freezes it with SIGSTOP on PAUSE. The script prints which of the agent-server's events the app server never received.

```sh
cp .pr/issue-616/demo_616.py tests/
DEMO_OUT=out.txt uv run pytest tests/demo_616.py -s
AUTOMATION_SANDBOX_CLEANUP_DELAY_SECONDS=600 DEMO_OUT=out.txt uv run pytest tests/demo_616.py -s
```

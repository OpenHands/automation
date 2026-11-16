# #673 evidence

`demo_673.py` drives the real `dispatch_pending_runs` / `_execute_run` against
Postgres. Five cron runs fire at once; a fake sandbox API admits two concurrent
sandboxes and answers the rest with the org concurrency-limit 429. Copy it into
`tests/` and run `DEMO_OUT=out.txt uv run pytest tests/demo_673.py`
(`DEMO_CAP=2` sets `AUTOMATION_MAX_CONCURRENT_RUNS`).

- `out-main.txt` / `repro-673-main.png`: main @ 949c001, 3 of 5 runs SKIPPED for good.
- `out-pr.txt`, `out-pr-cap2.txt` / `fixed-673-pr.png`: this branch, 5 of 5 complete.

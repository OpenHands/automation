# #675 evidence

`repro_675.sh <repo>` starts two independent local installations (agent-server
1.62.0 plus the automation service in local mode, each with its own SQLite DB),
dispatches a custom-tarball run in each (`probe_main.py` records
`AUTOMATION_USER_ID`), then restarts installation A and dispatches again.
`start_install.sh` expects an agent-server venv at `~/work/as-venv`.

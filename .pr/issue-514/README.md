# #514 evidence

`repro_514.sh <repo>` starts a local installation (agent-server 1.62.0 plus the automation service in local mode with SQLite), starts an unrelated `sleep 6001` BashCommand on the agent-server, dispatches a run whose entrypoint is `worker.py` (heartbeat once a second), cancels the run, and checks which processes survive and the exit code the agent-server reports for the entrypoint command. `start_install.sh` expects an agent-server venv at `~/work/as-venv`.

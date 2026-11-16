#!/bin/bash
# start_install.sh <repo_dir> <install_dir> <service_port> <agent_server_port>
# Starts one independent local installation: an agent-server and an automation
# service in local mode with its own SQLite DB, storage and workspace.
REPO=$1; INST=$2; SPORT=$3; APORT=$4
for p in $SPORT $APORT; do curl -s -o /dev/null http://127.0.0.1:$p/ && { echo "port $p busy"; exit 1; }; done
mkdir -p $INST/storage $INST/workspace $INST/home
cd $REPO
OH_SESSION_API_KEYS_0=dummy-agent-key OPENHANDS_SUPPRESS_BANNER=1 \
  nohup $HOME/work/as-venv/bin/agent-server --host 127.0.0.1 --port $APORT > $INST/agent-server.log 2>&1 &
echo $! > $INST/agent-server.pid
AUTOMATION_DB_URL=sqlite+aiosqlite:///$INST/automations.db \
AUTOMATION_AGENT_SERVER_URL=http://127.0.0.1:$APORT \
AUTOMATION_AGENT_SERVER_API_KEY=dummy-agent-key \
AUTOMATION_LOCAL_API_KEY=dummy-local-key \
AUTOMATION_WORKSPACE_BASE=$INST/workspace \
AUTOMATION_SERVER_PORT=$SPORT \
AUTOMATION_DISPATCHER_INTERVAL_SECONDS=2 \
LOCAL_STORAGE_PATH=$INST/storage LOG_JSON=0 \
  nohup .venv/bin/uvicorn openhands.automation.app:app --host 127.0.0.1 --port $SPORT > $INST/service.log 2>&1 &
echo $! > $INST/service.pid
for i in $(seq 1 60); do curl -sf http://127.0.0.1:$SPORT/health >/dev/null 2>&1 && curl -sf http://127.0.0.1:$APORT/health >/dev/null 2>&1 && { echo "install $INST up"; exit 0; }; sleep 1; done
echo "timeout"; tail -20 $INST/service.log; tail -5 $INST/agent-server.log; exit 1

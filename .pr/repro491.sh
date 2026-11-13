#!/bin/bash
# automation#491 reproduction attempt: git clone over SSH (local sshd, ~9 MB repo)
# 1) the issue's standalone asyncio.create_subprocess_exec script
# 2) Git Sync through the running automation service (uvicorn)
# usage: repro491.sh <checkout> <tag>
REPO=$1; TAG=$2; rm -rf ~/run/$TAG
export GIT_SSH_COMMAND="$HOME/sshtest/ssh_wrap.sh -o BatchMode=yes"
URL="ssh://openhands@127.0.0.1:2222/home/openhands/sshtest/repo.git"
echo "checkout: $(git -C $REPO rev-parse --short HEAD)   $(uname -s) $(uname -r | cut -d- -f1)   $($REPO/.venv/bin/python --version)   remote: $URL"
echo "\$ python repro_from_issue.py   # asyncio.create_subprocess_exec git clone, 25s timeout"
echo "  $($REPO/.venv/bin/python ~/sshtest/repro491.py $URL)"
export AUTOMATION_GIT_SYNC_REPO_URL=$URL AUTOMATION_GIT_SYNC_BRANCH=gitsync-test
~/run/start_stack.sh $REPO $TAG >/dev/null 2>&1
until curl -sf localhost:8010/ready >/dev/null; do sleep 2; done
H="Authorization: Bearer dev-local-key"; B=localhost:8010/api/automation/v1
echo "\$ curl -X POST $B/git-sync/sync   # service under uvicorn"; curl -s -X POST -H "$H" $B/git-sync/sync; echo
START=$(date +%s); sleep 2
while curl -s -H "$H" $B/git-sync/status | grep -q '"sync_in_progress":true'; do sleep 1; done
echo "  (sync cycle finished after $(( $(date +%s) - START + 2 ))s)"
curl -s -H "$H" $B/git-sync/status | python3 -c '
import json,sys; d=json.load(sys.stdin)
for k in ["last_synced_commit","last_error"]:
    print(f"  {k}: {d.get(k)}")'
kill $(cat ~/run/$TAG/*.pid)

#!/bin/bash
# Git Sync against a ~9 MB repo served over SSH through a ~50 KB/s link (a
# "normal-sized repo"), first sync with the default timeout.
# usage: repro492.sh <checkout> <tag>
REPO=$1; TAG=$2; rm -rf ~/run/$TAG
export GIT_SSH_COMMAND=$HOME/sshtest/ssh_slow.sh
export AUTOMATION_GIT_SYNC_REPO_URL="ssh://openhands@127.0.0.1:2222/home/openhands/sshtest/repo.git" AUTOMATION_GIT_SYNC_BRANCH=gitsync-test
~/run/start_stack.sh $REPO $TAG >/dev/null 2>&1
until curl -sf localhost:8010/ready >/dev/null; do sleep 2; done
H="Authorization: Bearer dev-local-key"; B=localhost:8010/api/automation/v1
echo "checkout: $(git -C $REPO rev-parse --short HEAD)   remote: ~9 MB repo over SSH at ~50 KB/s"
echo "\$ curl -X POST $B/git-sync/sync"; curl -s -X POST -H "$H" $B/git-sync/sync; echo
START=$(date +%s)
sleep 3
while curl -s -H "$H" $B/git-sync/status | grep -q '"sync_in_progress":true'; do sleep 3; done
echo "(sync cycle finished after $(( $(date +%s) - START ))s)"
echo "\$ curl $B/git-sync/status"
curl -s -H "$H" $B/git-sync/status | python3 -c '
import json,sys; d=json.load(sys.stdin)
for k in ["last_synced_commit","last_error","git_timeout_seconds"]:
    v = d.get(k, "<field MISSING from API>"); print(f"  {k}: {v}")
'
echo "\$ curl -X PUT $B/git-sync/config -d '{\"git_timeout_seconds\": 900}'"
curl -s -X PUT -H "$H" -H "Content-Type: application/json" -d '{"git_timeout_seconds": 900}' $B/git-sync/config | python3 -c '
import json,sys; d=json.load(sys.stdin); print("  git_timeout_seconds after PUT:", d.get("git_timeout_seconds", "<field MISSING: setting ignored, env var + restart is the only way>"))'
kill $(cat ~/run/$TAG/*.pid)

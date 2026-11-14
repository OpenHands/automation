#!/bin/bash
# automation#337: a finished run's logs and exit-code verification live only in agent-server bash events
# usage: repro337.sh <checkout> <tag>
REPO=$1; TAG=$2; rm -rf ~/run/$TAG
export AUTOMATION_RUN_TIMEOUT_MARGIN=5 AUTOMATION_RUN_TIMEOUT_HARD_GRACE=20 AUTOMATION_SANDBOX_READY_TIMEOUT=30
~/run/start_stack.sh $REPO $TAG >/dev/null 2>&1
until curl -sf localhost:8010/ready >/dev/null && curl -sf localhost:8011/health >/dev/null; do sleep 2; done
H="Authorization: Bearer dev-local-key"; B=localhost:8010/api/automation/v1; AS="X-Session-API-Key: dev-session-key"
echo "checkout: $(git -C $REPO rev-parse --short HEAD)"
TP=$(curl -s -X POST -H "$H" -H "Content-Type: application/gzip" --data-binary @$HOME/run/t337/bundle.tar.gz "$B/uploads?name=logs-demo" | python3 -c 'import json,sys;print(json.load(sys.stdin)["tarball_path"])')
mk() { curl -s -X POST -H "$H" -H "Content-Type: application/json" $B -d "{\"name\":\"$1\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"$2\",\"timeout\":15}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])'; }
run_of() { curl -s -H "$H" "$B/$1/runs" | python3 -c 'import json,sys;r=json.load(sys.stdin)["runs"];r=r[0] if r else {};print(r.get("status","NONE"), r.get("bash_command_id"), json.dumps(r.get("error_detail")))'; }
logs() { curl -s -H "$AS" "localhost:8011/api/bash/bash_events/search?kind__eq=BashOutput&command_id__eq=$1&limit=100" | python3 -c '
import json,sys
items=json.load(sys.stdin)["items"]
if not items: print("    (no BashOutput events) -> run-logs modal: logs unavailable")
for e in items:
    for l in (e.get("stdout") or "").splitlines(): print("    "+l)
    if e.get("exit_code") is not None: print("    exit_code:", e["exit_code"])'; }
wait_done() { for i in $(seq 90); do S=$(run_of $1 | cut -d" " -f1); [ "$S" = COMPLETED ] || [ "$S" = FAILED ] && break; sleep 2; done; }

echo "== Run A: script exits 0 and sends its completion callback"
A=$(mk run-a "python3 main.py --callback"); curl -s -o /dev/null -X POST -H "$H" $B/$A/dispatch; wait_done $A
read SA CA EA <<<"$(run_of $A)"; echo "  run status: $SA"
echo "  run logs (GET /api/bash/bash_events/search?command_id__eq=<bash_command_id>):"; logs $CA
echo "\$ curl -X DELETE localhost:8011/api/bash/bash_events   # same as OH_BASH_EVENTS_RETENTION_SECONDS pruning"
curl -s -o /dev/null -w "  HTTP %{http_code}\n" -X DELETE -H "$AS" localhost:8011/api/bash/bash_events
read SA CA EA <<<"$(run_of $A)"; echo "  run status still: $SA, but run logs now:"; logs $CA

echo "== Run B: script exits 0 but its callback is lost (no --callback); watchdog must verify the exit code"
Bid=$(mk run-b "python3 main.py"); curl -s -o /dev/null -X POST -H "$H" $B/$Bid/dispatch
for i in $(seq 60); do read SB CB EB <<<"$(run_of $Bid)"; [ "$CB" != None ] && [ -n "$(logs $CB | grep exit_code)" ] && break; sleep 1; done
echo "  process finished:"; logs $CB
echo "\$ curl -X DELETE localhost:8011/api/bash/bash_events   # pruned before the watchdog looks"
curl -s -o /dev/null -w "  HTTP %{http_code}\n" -X DELETE -H "$AS" localhost:8011/api/bash/bash_events
wait_done $Bid
read SB CB EB <<<"$(run_of $Bid)"; echo "  watchdog verdict -> run status: $SB"; echo "  error_detail: $EB"
kill $(cat ~/run/$TAG/*.pid)

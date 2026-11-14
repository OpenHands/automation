#!/bin/bash
# repro_514.sh <repo_dir>: a local installation (agent-server 1.62.0 + automation service in
# local mode) dispatches a run whose entrypoint is a long-running `python3 worker.py`. A second,
# unrelated `sleep 600` BashCommand runs on the same agent-server. The run is then cancelled.
REPO=$1; L=$HOME/work/local; I=/tmp/514; P=18001; A=18101
rm -rf $I; pkill -f 'python3 worker.py' 2>/dev/null; pkill -f 'sleep 6001' 2>/dev/null
$L/start_install.sh $REPO $I $P $A >/dev/null || exit 1
H="Authorization: Bearer dummy-local-key"; B=http://127.0.0.1:$P/api/automation/v1
TP=$(curl -s -X POST "$B/uploads?name=worker" -H "$H" -H "Content-Type: application/gzip" --data-binary @$L/worker.tar.gz | python3 -c 'import sys,json;print(json.load(sys.stdin)["tarball_path"])')
AID=$(curl -s -X POST "$B" -H "$H" -H "Content-Type: application/json" -d "{\"name\":\"long worker\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\",\"timezone\":\"UTC\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"python3 worker.py\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
OTHER=$(curl -s -X POST http://127.0.0.1:$A/api/bash/start_bash_command -H "X-Session-API-Key: dummy-agent-key" -H "Content-Type: application/json" -d '{"command":"sleep 6001"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
RID=$(curl -s -X POST "$B/$AID/dispatch" -H "$H" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
until pgrep -f 'python3 worker.py' >/dev/null; do sleep 1; done; sleep 3
echo "\$ pgrep -af 'python3 worker.py|sleep 6001'   # before cancel"
pgrep -af 'python3 worker.py|sleep 6001' | cut -c1-120 | sed 's/^/  /'
echo "\$ curl -X POST $B/runs/<run>/cancel"
curl -s -X POST "$B/runs/$RID/cancel" -H "$H" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  ->", {k:d.get(k) for k in ("status","completed_at")})'
sleep 5
echo "\$ sleep 5; pgrep -af 'python3 worker.py|sleep 6001'   # after cancel"
pgrep -af 'python3 worker.py|sleep 6001' | cut -c1-120 | sed 's/^/  /'
N1=$(wc -l < $I/workspace/heartbeat.txt); sleep 3; N2=$(wc -l < $I/workspace/heartbeat.txt)
CMD=$(sqlite3 $I/automations.db "select bash_command_id from automation_runs where id='${RID//-/}' or id='$RID'" 2>/dev/null)
CMD=$(python3 -c "import sqlite3;print(sqlite3.connect('$I/automations.db').execute('select bash_command_id from automation_runs').fetchone()[0])")
EXIT=$(curl -s "http://127.0.0.1:$A/api/bash/bash_events/search?kind__eq=BashOutput&command_id__eq=$CMD&sort_order=TIMESTAMP_DESC&limit=1" -H "X-Session-API-Key: dummy-agent-key" | python3 -c 'import sys,json;i=json.load(sys.stdin)["items"];print(i[0].get("exit_code") if i else "none yet, command still running")')
echo "entrypoint BashCommand exit_code on the agent-server: $EXIT"
echo "run status: $(curl -s "$B/$AID/runs" -H "$H" | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("items") or d.get("runs"))[0]["status"])')"
echo
if pgrep -f 'python3 worker.py' >/dev/null && [ $N2 -gt $N1 ]; then echo "BUG: run is CANCELLED but its entrypoint is still running (heartbeat $N1 -> $N2 lines in 3 s)"; else echo "OK: the cancelled run's entrypoint exited (heartbeat stopped at $N2 lines)"; fi
if pgrep -f 'sleep 6001' >/dev/null; then echo "OK: the unrelated BashCommand on the shared agent-server is still running"; else echo "BUG: the unrelated BashCommand was stopped too"; fi
pkill -f 'python3 worker.py' 2>/dev/null; pkill -f 'sleep 6001' 2>/dev/null; $L/stop_install.sh $I

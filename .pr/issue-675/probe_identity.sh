#!/bin/bash
# probe_identity.sh <service_port>: create a custom-tarball automation whose entrypoint
# records AUTOMATION_USER_ID, dispatch it, wait for COMPLETED.
P=$1; H="Authorization: Bearer dummy-local-key"; B=http://127.0.0.1:$P/api/automation/v1
TP=$(curl -s -X POST "$B/uploads?name=probe" -H "$H" -H "Content-Type: application/gzip" --data-binary @$HOME/work/local/probe.tar.gz | python3 -c 'import sys,json;print(json.load(sys.stdin)["tarball_path"])')
AID=$(curl -s -X POST "$B" -H "$H" -H "Content-Type: application/json" -d "{\"name\":\"identity probe\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\",\"timezone\":\"UTC\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"python3 main.py\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
RID=$(curl -s -X POST "$B/$AID/dispatch" -H "$H" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id") or d.get("run_id") or d)')
for i in $(seq 1 60); do S=$(curl -s "$B/$AID/runs" -H "$H" | python3 -c 'import sys,json;d=json.load(sys.stdin);r=(d.get("items") or d.get("runs") or d)[0];print(r["status"])'); [ "$S" = COMPLETED -o "$S" = FAILED ] && break; sleep 1; done
echo "run $RID -> $S"

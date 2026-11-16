#!/bin/bash
# automation#343: custom tarball automation whose setup script is scripts/install.sh
# usage: repro343.sh <checkout> <tag>
REPO=$1; TAG=$2; rm -rf ~/run/$TAG
~/run/start_stack.sh $REPO $TAG >/dev/null 2>&1
until curl -sf localhost:8010/ready >/dev/null && curl -sf localhost:8011/health >/dev/null; do sleep 2; done
H="Authorization: Bearer dev-local-key"; B=localhost:8010/api/automation/v1
echo "checkout: $(git -C $REPO rev-parse --short HEAD)"
echo "\$ tar tzf bundle.tar.gz"; tar tzf ~/run/t343/bundle.tar.gz | grep -v '/$' | sed 's/^/  /'
TP=$(curl -s -X POST -H "$H" -H "Content-Type: application/gzip" --data-binary @$HOME/run/t343/bundle.tar.gz "$B/uploads?name=setup-path-demo" | python3 -c 'import json,sys;print(json.load(sys.stdin)["tarball_path"])')
echo "\$ curl -X POST $B -d '{..., \"setup_script_path\": \"scripts/install.sh\", \"entrypoint\": \"python3 main.py\"}'"
AID=$(curl -s -X POST -H "$H" -H "Content-Type: application/json" $B -d "{\"name\":\"setup-path-demo\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"tarball_path\":\"$TP\",\"entrypoint\":\"python3 main.py\",\"setup_script_path\":\"scripts/install.sh\"}" | tee /tmp/a343.json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["id"]);print("  stored setup_script_path:", d["setup_script_path"], file=sys.stderr)')
echo "\$ curl -X POST $B/<id>/dispatch"; curl -s -o /dev/null -w "  HTTP %{http_code}\n" -X POST -H "$H" $B/$AID/dispatch
for i in $(seq 60); do
  ST=$(curl -s -H "$H" "$B/$AID/runs" | python3 -c 'import json,sys;r=json.load(sys.stdin)["runs"];print(r[0]["status"] if r else "NONE")' 2>/dev/null)
  [ "$ST" = COMPLETED ] || [ "$ST" = FAILED ] && break; sleep 2; done
echo "\$ curl $B/<id>/runs"
curl -s -H "$H" "$B/$AID/runs" | python3 -c '
import json,sys; r=json.load(sys.stdin)["runs"][0]
print("  status:", r["status"]); print("  error_detail:", r.get("error_detail"))'
echo "\$ agent-server bash output of the run (GET /api/bash/bash_events/search)"
curl -s -H "X-Session-API-Key: dev-session-key" "localhost:8011/api/bash/bash_events/search?limit=100" | python3 -c '
import json,sys
outs=[e for e in json.load(sys.stdin)["items"] if e.get("kind")=="BashOutput"]
for e in outs[-1:]:
    for line in ((e.get("stdout") or "")+(e.get("stderr") or "")).splitlines()[:4]: print("  "+line)
    print("  exit_code:", e.get("exit_code"))'
kill $(cat ~/run/$TAG/*.pid)

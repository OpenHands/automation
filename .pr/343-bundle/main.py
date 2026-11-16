import json, os, sys, urllib.request
def cb(status, error=None):
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error: body["error"] = error
    urllib.request.urlopen(urllib.request.Request(os.environ["AUTOMATION_CALLBACK_URL"], data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY','')}"}))
if os.path.exists(".deps_installed"):
    print("[main] dependencies present"); cb("COMPLETED")
else:
    msg = "ModuleNotFoundError: dependencies missing - setup script scripts/install.sh never ran"
    print(msg, file=sys.stderr); cb("FAILED", msg); sys.exit(1)

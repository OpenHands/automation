"""Record the attribution identity this run would hand the conversation."""
import json, os, urllib.request

out = os.environ["PROBE_OUT"] if "PROBE_OUT" in os.environ else os.path.join(os.environ["WORKSPACE_BASE"], "..", "..", "observed.txt")
with open(os.path.abspath(out), "a") as f:
    f.write(os.environ.get("AUTOMATION_USER_ID", "<unset>") + "\n")
print("AUTOMATION_USER_ID =", os.environ.get("AUTOMATION_USER_ID", "<unset>"))
req = urllib.request.Request(
    os.environ["AUTOMATION_CALLBACK_URL"],
    data=json.dumps({"status": "COMPLETED", "run_id": os.environ["AUTOMATION_RUN_ID"]}).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}"},
)
urllib.request.urlopen(req).close()

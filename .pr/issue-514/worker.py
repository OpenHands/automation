"""Long-running entrypoint: heartbeat once a second for 10 minutes."""
import os, time
beat = os.path.join(os.environ["WORKSPACE_BASE"], "..", "..", "heartbeat.txt")
for i in range(600):
    with open(beat, "a") as f:
        f.write(f"{time.time():.0f}\n")
    time.sleep(1)

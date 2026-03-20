import os

import httpx

print(f"HTTPX={httpx.__version__}")

g = os.environ.get
print(f"CALLBACK={g('AUTOMATION_CALLBACK_URL', 'MISSING')}")
print(f"RUN_ID={g('AUTOMATION_RUN_ID', 'MISSING')}")
print(f"SECRET={g('MY_SECRET', 'MISSING')}")
print("ALL_OK")

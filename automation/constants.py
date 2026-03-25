"""Constants for the automation service."""

from datetime import timedelta

# Maximum allowed timeout for automation runs (10 minutes)
# Used for:
# - Validation in API requests (timeout field)
# - Default timeout passed to sandbox execution
# - Watchdog staleness detection
MAX_RUN_DURATION = timedelta(minutes=10)
MAX_RUN_DURATION_SECONDS = int(MAX_RUN_DURATION.total_seconds())

# Sandbox execution constants
SANDBOX_POLL_INTERVAL = 5  # seconds between status checks
SANDBOX_READY_TIMEOUT = 300  # max wait for sandbox to become ready
WORK_DIR = "/workspace/automation"
TARBALL_PATH = "/tmp/automation.tar.gz"

# Limits for external tarball downloads (in sandbox)
EXTERNAL_DOWNLOAD_TIMEOUT = 120  # seconds
EXTERNAL_MAX_FILESIZE = 100 * 1024 * 1024  # 100 MB

# Rate limit retry settings
RATE_LIMIT_MIN_WAIT = 10  # initial wait after a 429
RATE_LIMIT_MAX_WAIT = 60  # max wait between retries
RATE_LIMIT_MAX_RETRIES = 5

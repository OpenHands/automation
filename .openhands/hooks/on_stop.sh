#!/bin/bash
# Stop hook: runs pre-commit before allowing agent to finish
#
# This hook runs when the agent attempts to stop/finish.
# It can BLOCK the stop by:
#   - Exiting with code 2 (blocked)
#   - Outputting JSON: {"decision": "deny", "additionalContext": "feedback message"}
#
# Environment variables available:
#   OPENHANDS_PROJECT_DIR - Project directory
#   OPENHANDS_SESSION_ID - Session ID
#   GITHUB_TOKEN - GitHub API token (if available)

set -o pipefail

PROJECT_DIR="${OPENHANDS_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_DIR" || exit 1

# Collect all issues to report back to the agent
ISSUES=""
BLOCK_STOP=false

log_issue() {
    ISSUES="${ISSUES}${1}\n"
    BLOCK_STOP=true
}

>&2 echo "=== Stop Hook ==="
>&2 echo "Project directory: $PROJECT_DIR"
>&2 echo ""

# --------------------------
# Step 1: Run pre-commit on all files
# --------------------------
>&2 echo "=== Running pre-commit run --all-files ==="
if command -v uv &> /dev/null; then
    PRECOMMIT_OUTPUT=$(uv run pre-commit run --all-files 2>&1)
    PRECOMMIT_EXIT=$?
else
    PRECOMMIT_OUTPUT=$(pre-commit run --all-files 2>&1)
    PRECOMMIT_EXIT=$?
fi

>&2 echo "$PRECOMMIT_OUTPUT"

if [ $PRECOMMIT_EXIT -ne 0 ]; then
    >&2 echo "⚠️  pre-commit found issues (exit code: $PRECOMMIT_EXIT)"
    log_issue "## Pre-commit Failed\n\nPre-commit checks failed. Please fix the following issues:\n\n\`\`\`\n${PRECOMMIT_OUTPUT}\n\`\`\`"
else
    >&2 echo "✓ pre-commit passed"
fi
>&2 echo ""

# --------------------------
# Final decision
# --------------------------
if [ "$BLOCK_STOP" = true ]; then
    >&2 echo "=== BLOCKING STOP: Issues found ==="
    # Output JSON to provide feedback to the agent
    # Escape the issues for JSON
    ESCAPED_ISSUES=$(echo -e "$ISSUES" | jq -Rs .)
    echo "{\"decision\": \"deny\", \"reason\": \"Pre-commit checks failed\", \"additionalContext\": $ESCAPED_ISSUES}"
    exit 2
fi

>&2 echo "=== All checks passed, allowing stop ==="
echo '{"decision": "allow"}'
exit 0

---
triggers:
  - /codereview
---

# Custom Code Review Guidelines for OpenHands/automation

## Review Submission Mode

When your review concludes with a verdict of "Worth merging" (✅) and the risk
assessment is 🟢 LOW with no critical issues and no unresolved review threads:

- **Submit the review as APPROVED** (not COMMENTED).
- Do not leave non-blocking observations as inline comments if they are the
  only thing preventing approval. Include them in the review body instead.
- If you find no blocking issues, the correct action is to approve the PR so
  it can proceed to merge.

A review that says "Worth merging" but is submitted as COMMENTED does not
satisfy the repository's required review gate and leaves the PR blocked
indefinitely. Always match your submission action to your verdict.

## Repository Context

This repository uses GitHub branch protection rules that require at least one
approval review. The `all-hands-bot` is a collaborator with write access and
is the designated automated reviewer. Its approval satisfies the independent
review requirement.

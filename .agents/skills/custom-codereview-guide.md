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

## Design Docs for Deep PRs

A diff shows what changed line by line, not the design: the shape of the
change, the API before and after, and why this approach. For a *deep* PR,
expect a short design doc. The `pr-design-doc` skill in
`.agents/skills/pr-design-doc/` produces a self-contained `.pr/` HTML page
(big picture plus before/after, grounded to real code) linked from the PR
description.

A PR is "deep" when a reviewer cannot fully judge it from the diff in a couple
of minutes, for example:

- a new or changed automation contract, webhook/event payload, or dispatch API;
- a new module or subsystem, or a cross-cutting refactor or migration;
- a behavior change in core logic (scheduling, run history, dispatch flow); or
- a large diff (roughly 500+ lines changed) whose intent a reviewer cannot hold
  in their head at once, even if no single hunk is complex.

Skip it for trivial PRs — a typo, a one-line guard, a config or dependency
bump, a docs tweak, a small localized bug fix. If the diff is its own
explanation, do not ask for a page.

When a deep PR ships without a design doc, weigh the omission against the
change's risk assessment:

- **🔴 HIGH risk and deep, no design doc:** withhold approval. Submit the
  review as COMMENTED and ask for a design doc (or an equivalent write-up in
  the PR description) so a human can judge the proposal before merge.
- **🟡 MEDIUM risk and deep, no design doc:** use judgment. Prefer to withhold
  approval and request one when the change is hard to reconstruct from the
  diff; a MEDIUM change that is small and self-evident does not need a page.
- **🟢 LOW risk:** never block on a missing design doc.

A design doc is a review aid, not a merge gate by itself. A well-written doc
does not excuse real correctness, security, or architecture problems.

## Repository Context

This repository uses GitHub branch protection rules that require at least one
approval review. The `all-hands-bot` is a collaborator with write access and
is the designated automated reviewer. Its approval satisfies the independent
review requirement.

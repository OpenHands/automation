"""Gate a pull request on the readiness of its linked issues.

A PR that links issues (`Fixes #123`, `Closes #123`, `Resolves #123`, or a bare
`#123` inside an `## Issue Number` section) may only merge once every linked
issue carries the `ready-for-dev` label — see issue-readiness-check.yml, which
applies that label when an issue meets the type-specific readiness criteria.

Issues created before the `ready-for-dev` rollout are grandfathered: the
issue-readiness workflow only labels issues on `issues` events, so issues that
predate it were never evaluated. Requiring the label retroactively would block
PRs linked to those issues. The cutoff is the UTC day AFTER the rollout, so
every issue predating deployment — including ones opened earlier that same day,
before the workflow existed — is exempt.

A PR that links no issues passes: many legitimate PRs (dependency bumps, small
chores) have no tracking issue, and this gate's purpose is to keep linked work
honest, not to force a link.

Local usage:

    python .github/scripts/check_pr_description.py --body-file /tmp/pr-body.md
    python .github/scripts/check_pr_description.py --event-path "$GITHUB_EVENT_PATH"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


HEADING_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
ISSUE_REF_RE = re.compile(
    r"(?i)\b(?:fix(?:es|ed)|clos(?:es|ed)|resolv(?:es|ed))\s+#(\d+)"
)
FENCED_CODE_RE = re.compile(r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$")
BLOCKQUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
BARE_ISSUE_REF_RE = re.compile(r"(?<!\w)#(\d+)")
READY_FOR_DEV_LABEL = "ready-for-dev"
READY_FOR_DEV_ROLLOUT_ISO = "2026-09-04"


def extract_sections(body: str) -> dict[str, str]:
    matches = list(HEADING_RE.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip().lower()] = body[start:end]
    return sections


def extract_linked_issue_numbers(body: str) -> list[int]:
    numbers: list[int] = []
    seen: set[int] = set()
    searchable_body = FENCED_CODE_RE.sub("", body)
    searchable_body = BLOCKQUOTE_LINE_RE.sub("", searchable_body)
    for match in ISSUE_REF_RE.finditer(searchable_body):
        number = int(match.group(1))
        if number not in seen:
            numbers.append(number)
            seen.add(number)

    sections = extract_sections(searchable_body)
    issue_section = sections.get("issue number", "")
    for match in BARE_ISSUE_REF_RE.finditer(issue_section):
        number = int(match.group(1))
        if number not in seen:
            numbers.append(number)
            seen.add(number)
    return numbers


def fetch_issue_details(
    repo: str, issue_number: int, token: str
) -> tuple[list[str], str]:
    """Return an issue's (labels, created_at) from the GitHub API."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - trusted HTTPS API
        issue = json.loads(response.read().decode())
    labels = [
        label["name"] for label in issue.get("labels", []) if isinstance(label, dict)
    ]
    created_at = issue.get("created_at", "")
    return labels, created_at


def validate_linked_issue_ready(
    body: str, repo: str | None = None, token: str | None = None
) -> list[str]:
    numbers = extract_linked_issue_numbers(body)
    if not numbers:
        return []
    if not repo:
        return [
            "Repository identity is unavailable; linked issues cannot be validated."
        ]
    if not token:
        return ["GITHUB_TOKEN is unavailable; linked issues cannot be validated."]

    missing: list[int] = []
    not_ready_new: list[int] = []
    for number in numbers:
        try:
            labels, created_at = fetch_issue_details(repo, number, token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                missing.append(number)
                continue
            raise
        if READY_FOR_DEV_LABEL in (label.lower() for label in labels):
            continue
        if created_at[:10] < READY_FOR_DEV_ROLLOUT_ISO:
            # Predates the rollout; grandfathered to avoid retroactive blocking.
            continue
        not_ready_new.append(number)

    errors: list[str] = []
    if missing:
        refs = ", ".join(f"#{number}" for number in missing)
        errors.append(
            f"Referenced issue(s) {refs} could not be found in this repository."
        )
    if not_ready_new:
        refs = ", ".join(f"#{number}" for number in not_ready_new)
        errors.append(
            f"Linked issue(s) ({refs}) carry neither `ready-for-dev` nor a "
            "pre-rollout creation date. Newly referenced issues must meet the "
            "readiness criteria before a PR can be opened."
        )
    return errors


def body_from_event(event_path: Path) -> tuple[str, str | None]:
    """Return the (pull request body, repository full name) from an event payload."""
    payload = json.loads(event_path.read_text())
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("GitHub event payload does not contain a pull_request object")
    body = pull_request.get("body")
    body = body if isinstance(body, str) else ""
    repo = payload.get("repository", {}).get("full_name")
    return body, repo if isinstance(repo, str) else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate pull request linked-issue readiness from --body-file "
            "or a GitHub event payload."
        )
    )
    parser.add_argument(
        "--body-file", type=Path, help="Read a PR description body from a file."
    )
    parser.add_argument(
        "--event-path",
        type=Path,
        default=Path(os.environ["GITHUB_EVENT_PATH"])
        if "GITHUB_EVENT_PATH" in os.environ
        else None,
        help="Read the PR description body from a GitHub event payload.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.body_file is not None:
        body = args.body_file.read_text()
        errors: list[str] = []
    elif args.event_path is not None:
        body, repo = body_from_event(args.event_path)
        errors = validate_linked_issue_ready(body, repo, os.environ.get("GITHUB_TOKEN"))
    else:
        raise SystemExit("Pass --body-file or set GITHUB_EVENT_PATH.")

    for error in errors:
        print(f"::error::{error}")

    if errors:
        print(f"PR linked-issue validation failed with {len(errors)} error(s).")
        return 1

    print("PR linked-issue validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Clone repositories from repos_config.json.

This script is shared by both prompt and plugin presets. It:
1. Reads repos_config.json from the same directory as the script
2. Fetches git tokens from the sandbox settings API
3. Clones each repo to /workspace/project/repo_N (agent's working directory)

Environment variables used:
- SANDBOX_ID: Sandbox identifier for settings API
- SESSION_API_KEY: Auth key for settings API
- OPENHANDS_CLOUD_API_URL: Cloud API base URL
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# Configuration
REPOS_DIR = Path("/workspace/project")  # Clone repos into agent's working directory
CLONE_TIMEOUT = 300  # 5 minutes per repo


def get_secret(name: str) -> str | None:
    """Fetch a secret value from the sandbox settings API.

    Uses stdlib urllib.request to avoid dependency on httpx.
    """
    sandbox_id = os.environ.get("SANDBOX_ID", "")
    session_key = os.environ.get("SESSION_API_KEY", "")
    cloud_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "")

    if not (sandbox_id and session_key and cloud_url):
        return None
    try:
        req = urllib.request.Request(
            f"{cloud_url}/api/v1/sandboxes/{sandbox_id}/settings/secrets/{name}",
            headers={"X-Session-API-Key": session_key},
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8")
    except Exception as e:
        print(f"[clone] Could not fetch secret {name}: {e}", file=sys.stderr)
    return None


def is_commit_sha(ref: str | None) -> bool:
    """Check if ref looks like a git commit SHA."""
    if not ref:
        return False
    return bool(re.match(r"^[0-9a-f]{7,40}$", ref, re.IGNORECASE))


def build_clone_url(
    url: str, github_token: str | None, gitlab_token: str | None
) -> str:
    """Build authenticated clone URL based on the repository URL."""
    # Handle owner/repo format (assume GitHub)
    if "://" not in url and "/" in url and not url.startswith("git@"):
        if github_token:
            return f"https://{github_token}@github.com/{url}.git"
        return f"https://github.com/{url}.git"

    # Handle full URLs
    if "github.com" in url:
        if github_token:
            return url.replace(
                "https://github.com", f"https://{github_token}@github.com"
            )
        return url
    elif "gitlab.com" in url:
        if gitlab_token:
            return url.replace(
                "https://gitlab.com", f"https://oauth2:{gitlab_token}@gitlab.com"
            )
        return url

    # Return as-is for other URLs
    return url


def mask_url(url: str) -> str:
    """Remove credentials from URL for display."""
    if "://" not in url:
        return url
    return url.split("://")[0] + "://" + url.split("://")[-1].split("@")[-1]


def _mask_tokens(
    text: str, github_token: str | None, gitlab_token: str | None
) -> str:
    """Mask tokens in text for safe logging."""
    if github_token:
        text = text.replace(github_token, "***")
    if gitlab_token:
        text = text.replace(gitlab_token, "***")
    return text


def clone_repos(config_path: Path) -> tuple[int, list[str]]:
    """Clone repositories from config file.

    Args:
        config_path: Path to repos_config.json

    Returns:
        Tuple of (success_count, list of failed repo URLs for diagnostics)
    """
    if not config_path.exists():
        print("[clone] No repos_config.json found, skipping clone")
        return 0, []

    with open(config_path) as f:
        repos = json.load(f)

    if not repos:
        print("[clone] Empty repos config, skipping clone")
        return 0, []

    print(f"[clone] Cloning {len(repos)} repository(ies)...")

    # Create repos directory
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch tokens once
    failed_repos: list[str] = []
    github_token = get_secret("github_token")
    gitlab_token = get_secret("gitlab_token")

    success_count = 0
    for i, repo in enumerate(repos):
        url = repo.get("url", "")
        ref = repo.get("ref")
        dest = REPOS_DIR / f"repo_{i}"

        if not url:
            print(f"[clone] Skipping repo {i}: no URL specified", file=sys.stderr)
            continue

        clone_url = build_clone_url(url, github_token, gitlab_token)
        display_url = mask_url(url)

        # Build git clone command
        # Note: --depth 1 with --branch only works for branches/tags, not SHAs.
        # For SHA refs, we do a full clone then checkout the specific commit.
        if is_commit_sha(ref):
            # Full clone for SHA refs (shallow clone can't fetch arbitrary commits)
            cmd = ["git", "clone", clone_url, str(dest)]
            needs_checkout = True
        else:
            # Shallow clone for branches/tags
            cmd = ["git", "clone", "--depth", "1"]
            if ref:
                cmd.extend(["--branch", ref])
            cmd.extend([clone_url, str(dest)])
            needs_checkout = False

        print(f"[clone] Cloning {display_url} -> {dest}")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT
            )
            if result.returncode != 0:
                error_msg = _mask_tokens(result.stderr, github_token, gitlab_token)
                print(
                    f"[clone] WARNING: Failed to clone {display_url}: {error_msg}",
                    file=sys.stderr,
                )
                failed_repos.append(display_url)
                continue

            # Checkout specific SHA if needed
            if needs_checkout and ref:
                checkout_result = subprocess.run(
                    ["git", "-C", str(dest), "checkout", ref],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if checkout_result.returncode != 0:
                    print(
                        f"[clone] WARNING: Failed to checkout {ref}: "
                        f"{checkout_result.stderr}",
                        file=sys.stderr,
                    )
                    failed_repos.append(display_url)
                    continue

            print(f"[clone] Successfully cloned {display_url}")
            success_count += 1

        except subprocess.TimeoutExpired:
            print(
                f"[clone] WARNING: Clone timed out for {display_url}", file=sys.stderr
            )
            failed_repos.append(display_url)
            continue

    print(f"[clone] Cloned {success_count}/{len(repos)} repositories")
    if failed_repos:
        print(f"[clone] FAILED repos: {', '.join(failed_repos)}", file=sys.stderr)
    return success_count, failed_repos


def main() -> None:
    """Entry point - clone repos from config in script directory.

    Clone failures are non-fatal - warnings are printed but the script exits 0.
    This allows automations to proceed even if some repos fail to clone.
    """
    script_dir = Path(__file__).parent
    config_path = script_dir / "repos_config.json"
    clone_repos(config_path)


if __name__ == "__main__":
    main()

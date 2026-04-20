#!/usr/bin/env python3
"""Clone repositories from repos_config.json.

This script is shared by both prompt and plugin presets. It:
1. Reads repos_config.json from the same directory as the script
2. Fetches git tokens from the sandbox settings API
3. Clones each repo to /workspace/repos/repo_N

Environment variables used:
- SANDBOX_ID: Sandbox identifier for settings API
- SESSION_API_KEY: Auth key for settings API
- OPENHANDS_CLOUD_API_URL: Cloud API base URL
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Configuration
REPOS_DIR = Path("/workspace/repos")


def get_secret(name: str) -> str | None:
    """Fetch a secret value from the sandbox settings API."""
    sandbox_id = os.environ.get("SANDBOX_ID", "")
    session_key = os.environ.get("SESSION_API_KEY", "")
    cloud_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "")

    if not (sandbox_id and session_key and cloud_url):
        return None
    try:
        import httpx

        resp = httpx.get(
            f"{cloud_url}/api/v1/sandboxes/{sandbox_id}/settings/secrets/{name}",
            headers={"X-Session-API-Key": session_key},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        print(f"[clone] Could not fetch secret {name}: {e}", file=sys.stderr)
    return None


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


def clone_repos(config_path: Path) -> int:
    """Clone repositories from config file.

    Args:
        config_path: Path to repos_config.json

    Returns:
        Number of successfully cloned repositories
    """
    if not config_path.exists():
        print("[clone] No repos_config.json found, skipping clone")
        return 0

    with open(config_path) as f:
        repos = json.load(f)

    if not repos:
        print("[clone] Empty repos config, skipping clone")
        return 0

    print(f"[clone] Cloning {len(repos)} repository(ies)...")

    # Create repos directory
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch tokens once
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
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend([clone_url, str(dest)])

        print(f"[clone] Cloning {display_url} -> {dest}")

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Mask tokens in error message
            error_msg = result.stderr
            if github_token:
                error_msg = error_msg.replace(github_token, "***")
            if gitlab_token:
                error_msg = error_msg.replace(gitlab_token, "***")
            print(
                f"[clone] WARNING: Failed to clone {display_url}: {error_msg}",
                file=sys.stderr,
            )
        else:
            print(f"[clone] Successfully cloned {display_url}")
            success_count += 1

    print(f"[clone] Cloned {success_count}/{len(repos)} repositories")
    return success_count


def main() -> None:
    """Entry point - clone repos from config in script directory."""
    script_dir = Path(__file__).parent
    config_path = script_dir / "repos_config.json"
    clone_repos(config_path)


if __name__ == "__main__":
    main()

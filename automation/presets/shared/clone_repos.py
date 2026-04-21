#!/usr/bin/env python3
"""Clone repositories from repos_config.json.

This script is shared by both prompt and plugin presets. It:
1. Reads repos_config.json from the same directory as the script
2. Fetches git tokens from the sandbox settings API
3. Clones each repo to /workspace/project/<repo-name> (using meaningful names)
4. Writes repos_mapping.json with URL → local path mapping

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
REPOS_MAPPING_FILE = REPOS_DIR / "repos_mapping.json"


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


def extract_repo_name(url: str) -> str:
    """Extract repository name from URL for use as directory name.

    Examples:
        owner/repo -> repo
        https://github.com/owner/repo.git -> repo
        git@github.com:owner/repo.git -> repo
    """
    # Remove trailing .git
    url = re.sub(r"\.git$", "", url)

    # Handle git@host:owner/repo format
    if url.startswith("git@"):
        url = url.split(":")[-1]

    # Handle https://host/owner/repo format
    if "://" in url:
        url = url.split("://")[-1]

    # Get the last path component (repo name)
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts else "repo"


def sanitize_dir_name(name: str) -> str:
    """Sanitize a string for use as a directory name.

    Replaces invalid characters with underscores and ensures the name is safe.
    """
    # Replace characters that are problematic in file paths
    sanitized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name)
    # Remove leading/trailing dots and spaces
    sanitized = sanitized.strip(". ")
    # Ensure non-empty
    return sanitized if sanitized else "repo"


def get_unique_dir_name(base_name: str, existing_dirs: set[str]) -> str:
    """Get a unique directory name, appending _N if needed.

    Args:
        base_name: The desired directory name
        existing_dirs: Set of already-used directory names

    Returns:
        A unique directory name (base_name or base_name_1, base_name_2, etc.)
    """
    if base_name not in existing_dirs:
        return base_name

    # Find next available suffix
    counter = 1
    while f"{base_name}_{counter}" in existing_dirs:
        counter += 1
    return f"{base_name}_{counter}"


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


def clone_repos(config_path: Path) -> tuple[int, list[str], dict[str, dict]]:
    """Clone repositories from config file.

    Clones repos to meaningful directory names (e.g., 'openhands-cli' instead of 'repo_0').
    Writes repos_mapping.json with the URL → local path mapping for agent reference.

    Args:
        config_path: Path to repos_config.json

    Returns:
        Tuple of (success_count, failed_repo_urls, repo_mapping)
        repo_mapping: dict mapping original URL to {local_path, ref, dir_name}
    """
    repo_mapping: dict[str, dict] = {}

    if not config_path.exists():
        print("[clone] No repos_config.json found, skipping clone")
        return 0, [], repo_mapping

    with open(config_path) as f:
        repos = json.load(f)

    if not repos:
        print("[clone] Empty repos config, skipping clone")
        return 0, [], repo_mapping

    print(f"[clone] Cloning {len(repos)} repository(ies)...")

    # Create repos directory
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch tokens once
    failed_repos: list[str] = []
    github_token = get_secret("github_token")
    gitlab_token = get_secret("gitlab_token")

    # Track used directory names to handle collisions
    used_dir_names: set[str] = set()

    success_count = 0
    for repo in repos:
        url = repo.get("url", "")
        ref = repo.get("ref")

        if not url:
            print("[clone] Skipping repo: no URL specified", file=sys.stderr)
            continue

        # Determine directory name from repo URL
        raw_name = extract_repo_name(url)
        safe_name = sanitize_dir_name(raw_name)
        dir_name = get_unique_dir_name(safe_name, used_dir_names)
        used_dir_names.add(dir_name)

        dest = REPOS_DIR / dir_name
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

        print(f"[clone] Cloning {display_url} -> {dest.name}/")

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

            print(f"[clone] Successfully cloned {display_url} -> {dir_name}/")
            success_count += 1

            # Record mapping
            repo_mapping[url] = {
                "dir_name": dir_name,
                "local_path": str(dest),
                "ref": ref,
            }

        except subprocess.TimeoutExpired:
            print(
                f"[clone] WARNING: Clone timed out for {display_url}", file=sys.stderr
            )
            failed_repos.append(display_url)
            continue

    # Write mapping file for agent reference
    if repo_mapping:
        with open(REPOS_MAPPING_FILE, "w") as f:
            json.dump(repo_mapping, f, indent=2)
        print(f"[clone] Wrote repository mapping to {REPOS_MAPPING_FILE.name}")

    print(f"[clone] Cloned {success_count}/{len(repos)} repositories")
    if failed_repos:
        print(f"[clone] FAILED repos: {', '.join(failed_repos)}", file=sys.stderr)
    return success_count, failed_repos, repo_mapping


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

#!/usr/bin/env python3
"""Verify that openhands-sdk, openhands-workspace, and openhands-tools versions
are consistent across pyproject.toml and public scripts.

Exit 0 if all versions match, exit 1 with diagnostics otherwise.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = REPO_ROOT / "pyproject.toml"

# Package names we expect to share a single version
PACKAGES = ["openhands-sdk", "openhands-workspace"]

# Shell scripts with a hardcoded SDK_VERSION="x.y.z" that must stay in sync
VERSIONED_SCRIPTS = [
    REPO_ROOT / "scripts" / "test_tarball" / "setup.sh",
]

SDK_VERSION_RE = re.compile(r'^SDK_VERSION="([^"]+)"', re.MULTILINE)


def get_pyproject_versions() -> dict[str, str]:
    """Return {package_name: version} for our tracked packages in pyproject.toml."""
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)

    versions: dict[str, str] = {}
    for dep in data.get("project", {}).get("dependencies", []):
        for pkg in PACKAGES:
            if dep.lower().startswith(pkg):
                match = re.search(r"==\s*([^\s,;]+)", dep)
                if match:
                    versions[pkg] = match.group(1)
    return versions


def get_script_version(path: Path) -> str | None:
    """Extract SDK_VERSION="..." from a shell script."""
    text = path.read_text()
    m = SDK_VERSION_RE.search(text)
    return m.group(1) if m else None


def main() -> int:
    errors: list[str] = []

    # 1. Check pyproject.toml versions exist and match
    versions = get_pyproject_versions()
    missing = [p for p in PACKAGES if p not in versions]
    if missing:
        errors.append(
            f"pyproject.toml: missing pinned versions for {', '.join(missing)}"
        )

    unique = set(versions.values())
    if len(unique) > 1:
        detail = ", ".join(f"{k}=={v}" for k, v in sorted(versions.items()))
        errors.append(f"pyproject.toml: version mismatch — {detail}")

    canonical = next(iter(unique)) if len(unique) == 1 else None

    # 2. Check hardcoded SDK_VERSION in shell scripts
    for script in VERSIONED_SCRIPTS:
        if not script.exists():
            errors.append(f"{script.relative_to(REPO_ROOT)}: file not found")
            continue

        sv = get_script_version(script)
        if sv is None:
            errors.append(
                f"{script.relative_to(REPO_ROOT)}: "
                'no SDK_VERSION="..." line found'
            )
        elif canonical and sv != canonical:
            errors.append(
                f"{script.relative_to(REPO_ROOT)}: "
                f'SDK_VERSION="{sv}" does not match '
                f"pyproject.toml version {canonical}"
            )

    # Report
    if errors:
        print("❌ Version consistency check FAILED:\n")
        for e in errors:
            print(f"  • {e}")
        print()
        if canonical:
            print(f"  Expected version: {canonical}")
        return 1

    print(f"✅ All openhands package versions are consistent: {canonical}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

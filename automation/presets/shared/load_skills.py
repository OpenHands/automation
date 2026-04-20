#!/usr/bin/env python3
"""Load skills from cloned repositories.

This shared utility is used by both prompt and plugin sdk_main.py scripts
to load skills from cloned repos in /workspace/repos.
"""

import sys
from pathlib import Path

# Directory where repos are cloned by setup.sh
REPOS_DIR = Path("/workspace/repos")


def load_skills_from_repos() -> tuple[list, object | None]:
    """Load skills from all cloned repositories.

    Iterates over /workspace/repos/repo_* directories and loads skills
    from each using the SDK's load_project_skills function.

    Returns:
        Tuple of (list of loaded Skill objects, AgentContext or None)
    """
    # Import SDK modules (must be installed by setup.sh before this runs)
    from openhands.sdk.context import AgentContext
    from openhands.sdk.skills import Skill, load_project_skills

    loaded_skills: list[Skill] = []

    if not REPOS_DIR.exists():
        return loaded_skills, None

    print("\n=== LOADING SKILLS FROM REPOS ===")
    for repo_dir in sorted(REPOS_DIR.iterdir()):
        if repo_dir.is_dir():
            try:
                skills = load_project_skills(repo_dir)
                loaded_skills.extend(skills)
                print(f"  {repo_dir.name}: loaded {len(skills)} skill(s)")
                for skill in skills:
                    print(f"    - {skill.name}")
            except Exception as e:
                print(f"  {repo_dir.name}: WARNING - {e}", file=sys.stderr)
    print(f"  Total skills loaded: {len(loaded_skills)}")

    # Create AgentContext with loaded skills (if any)
    # Note: load_public_skills=True ensures repo skills are ADDITIVE to public skills
    agent_context = None
    if loaded_skills:
        agent_context = AgentContext(skills=loaded_skills, load_public_skills=True)

    return loaded_skills, agent_context

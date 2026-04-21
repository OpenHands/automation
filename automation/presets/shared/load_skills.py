#!/usr/bin/env python3
"""Load skills via the local agent-server's /api/skills endpoint.

This shared utility is used by both prompt and plugin sdk_main.py scripts
to load ALL skills (public, user, project, org) via the agent-server running
inside the sandbox. This mirrors how V1 conversations load skills in OpenHands.

The agent-server provides a unified skill loading interface that handles:
- Public skills (from OpenHands/skills GitHub repo, pre-packaged in image)
- User skills (from ~/.openhands/skills/)
- Project skills (from cloned repos: .agents/skills/, .openhands/microagents/)
- Organization skills (from org/.openhands repository, if configured)
- Sandbox skills (from exposed URLs, if configured)
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

# Directory where repos are cloned by setup.sh (agent's working directory)
PROJECT_DIR = Path("/workspace/project")

# Agent-server port (standard port used by OpenHands agent-server)
AGENT_SERVER_PORT = int(os.environ.get("AGENT_SERVER_PORT", "3000"))
AGENT_SERVER_URL = f"http://localhost:{AGENT_SERVER_PORT}"


def _load_skills_via_agent_server(
    project_dir: str,
    session_api_key: str | None = None,
    load_public: bool = True,
    load_user: bool = True,
    load_project: bool = True,
    load_org: bool = True,
    timeout: float = 60.0,
) -> list[dict]:
    """Call the agent-server's /api/skills endpoint to load all skills.

    Args:
        project_dir: Workspace directory path for project skills
        session_api_key: Session API key for authentication (optional)
        load_public: Whether to load public skills (default: True)
        load_user: Whether to load user skills (default: True)
        load_project: Whether to load project skills (default: True)
        load_org: Whether to load organization skills (default: True)
        timeout: Request timeout in seconds

    Returns:
        List of skill dicts from the agent-server response.
        Returns empty list on error.
    """
    # Build request payload (same format as OpenHands skill_loader.py)
    payload = {
        "load_public": load_public,
        "load_user": load_user,
        "load_project": load_project,
        "load_org": load_org,
        "project_dir": project_dir,
        "org_config": None,  # Could be added later for org-level skills
        "sandbox_config": None,  # Could be added later for sandbox skills
    }

    # Build headers
    headers = {"Content-Type": "application/json"}
    if session_api_key:
        headers["X-Session-API-Key"] = session_api_key

    try:
        req = urllib.request.Request(
            f"{AGENT_SERVER_URL}/api/skills",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                skills = data.get("skills", [])
                sources = data.get("sources", {})
                print(f"  Agent-server sources: {sources}")
                return skills
            else:
                print(
                    f"  WARNING: Agent-server returned status {resp.status}",
                    file=sys.stderr,
                )
                return []

    except urllib.error.HTTPError as e:
        print(
            f"  WARNING: Agent-server HTTP error {e.code}: {e.reason}",
            file=sys.stderr,
        )
        return []
    except urllib.error.URLError as e:
        print(f"  WARNING: Failed to connect to agent-server: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  WARNING: Failed to load skills via agent-server: {e}", file=sys.stderr)
        return []


def _convert_skill_info_to_skill(skill_data: dict):
    """Convert skill dict from API response to SDK Skill object.

    Args:
        skill_data: Dict with name, content, triggers, source, description, etc.

    Returns:
        Skill object
    """
    from openhands.sdk.skills import KeywordTrigger, Skill, TaskTrigger

    trigger = None
    triggers = skill_data.get("triggers", [])

    if triggers:
        # Determine trigger type based on content (same logic as OpenHands)
        if any(t.startswith("/") for t in triggers):
            trigger = TaskTrigger(triggers=triggers)
        else:
            trigger = KeywordTrigger(keywords=triggers)

    return Skill(
        name=skill_data.get("name", "unknown"),
        content=skill_data.get("content", ""),
        trigger=trigger,
        source=skill_data.get("source"),
        description=skill_data.get("description"),
        is_agentskills_format=skill_data.get("is_agentskills_format", False),
    )


def _find_cloned_repo_dirs() -> list[Path]:
    """Find all cloned repo directories (repo_0, repo_1, etc.).

    Returns:
        List of paths to cloned repo directories, sorted by name.
    """
    if not PROJECT_DIR.exists():
        return []

    repo_dirs = []
    for path in sorted(PROJECT_DIR.iterdir()):
        if path.is_dir() and path.name.startswith("repo_"):
            repo_dirs.append(path)

    return repo_dirs


def load_skills_from_agent_server() -> tuple[list, object | None]:
    """Load ALL skills via the local agent-server's /api/skills endpoint.

    This mirrors how V1 conversations load skills in OpenHands, providing
    access to public, user, project, and potentially org-level skills.

    For multiple cloned repos (repo_0, repo_1, etc.), project skills are
    loaded from EACH repo directory separately. Skills are deduplicated
    by name, with later repos taking precedence over earlier ones.

    Returns:
        Tuple of (list of loaded Skill objects, AgentContext or None)
    """
    from openhands.sdk.context import AgentContext
    from openhands.sdk.skills import Skill

    print("\n=== LOADING SKILLS VIA AGENT-SERVER ===")
    print(f"  Agent-server URL: {AGENT_SERVER_URL}")
    print(f"  Base directory: {PROJECT_DIR}")

    # Get session API key from environment (injected by dispatcher)
    session_api_key = os.environ.get("SESSION_API_KEY")

    # Use dict to deduplicate skills by name (later skills override earlier)
    skills_by_name: dict[str, dict] = {}

    # Find cloned repo directories
    repo_dirs = _find_cloned_repo_dirs()

    if repo_dirs:
        # Multiple repos cloned - load project skills from EACH repo
        print(f"  Found {len(repo_dirs)} cloned repo(s): {[d.name for d in repo_dirs]}")

        # First call: load public/user/org skills (not repo-specific)
        # Use first repo dir as project_dir but only load non-project skills
        print("  Loading public/user/org skills...")
        global_skill_dicts = _load_skills_via_agent_server(
            project_dir=str(repo_dirs[0]),
            session_api_key=session_api_key,
            load_public=True,
            load_user=True,
            load_project=False,  # Don't load project skills yet
            load_org=True,
        )
        for skill_data in global_skill_dicts:
            name = skill_data.get("name", "unknown")
            skills_by_name[name] = skill_data

        # Load project skills from EACH cloned repo
        for repo_dir in repo_dirs:
            print(f"  Loading project skills from {repo_dir.name}...")
            repo_skill_dicts = _load_skills_via_agent_server(
                project_dir=str(repo_dir),
                session_api_key=session_api_key,
                load_public=False,  # Already loaded
                load_user=False,    # Already loaded
                load_project=True,  # Load project skills for this repo
                load_org=False,     # Already loaded
            )
            for skill_data in repo_skill_dicts:
                name = skill_data.get("name", "unknown")
                # Later repos override earlier repos (intentional)
                skills_by_name[name] = skill_data
    else:
        # No cloned repos - load all skills from base project directory
        print("  No cloned repos found, loading from base directory...")
        skill_dicts = _load_skills_via_agent_server(
            project_dir=str(PROJECT_DIR),
            session_api_key=session_api_key,
            load_public=True,
            load_user=True,
            load_project=True,
            load_org=True,
        )
        for skill_data in skill_dicts:
            name = skill_data.get("name", "unknown")
            skills_by_name[name] = skill_data

    # Convert to SDK Skill objects
    loaded_skills: list[Skill] = []
    for skill_data in skills_by_name.values():
        try:
            skill = _convert_skill_info_to_skill(skill_data)
            loaded_skills.append(skill)
        except Exception as e:
            skill_name = skill_data.get("name", "unknown")
            print(f"  WARNING: Failed to convert skill {skill_name}: {e}", file=sys.stderr)

    print(f"  Total skills loaded: {len(loaded_skills)}")
    if loaded_skills:
        print("  Skills:")
        for skill in loaded_skills:
            source_info = f" ({skill.source})" if skill.source else ""
            print(f"    - {skill.name}{source_info}")

    # Create AgentContext with loaded skills (if any)
    agent_context = None
    if loaded_skills:
        # Note: We set load_public_skills=False since we already loaded public skills
        # via the agent-server. Setting True would cause duplicate loading.
        agent_context = AgentContext(skills=loaded_skills, load_public_skills=False)

    return loaded_skills, agent_context

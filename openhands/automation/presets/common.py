from openhands.sdk.settings import ACPAgentSettings
from openhands.tools.preset.default import get_default_agent


def resolve_agent(workspace, model_profile: str | None, cli_mode: bool = True):
    agent_settings = workspace._fetch_agent_settings()
    if isinstance(agent_settings, ACPAgentSettings):
        return agent_settings.create_agent()

    try:
        llm = workspace.get_llm(profile_name=model_profile)
    except FileNotFoundError:
        if not model_profile:
            raise
        llm = workspace.get_llm()

    return get_default_agent(llm=llm, cli_mode=cli_mode)

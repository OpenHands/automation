"""Prompt assembly helpers for plugin-spec conversation automations."""

import json
import os
from typing import Any


def outcome_context_section(environ=os.environ) -> str | None:
    if not environ.get("AUTOMATION_OUTCOME_URL") or not environ.get(
        "AUTOMATION_OUTCOME_TOKEN"
    ):
        return None
    return """## Outcome Reporting API

This automation can record structured outcome values for analytics. When you have
final outcome data to report, use terminal commands with the injected secrets
`$AUTOMATION_OUTCOME_URL` and `$AUTOMATION_OUTCOME_TOKEN`. POST JSON in this
shape: `{"payload": {...}}`. The service stores values selected by this
automation's `outcome_parsers`; if no parsers are configured, it accepts a flat
JSON object."""


def build_user_prompt(base_prompt: str, event_context: dict[str, Any] | None) -> str:
    context_sections: list[str] = []

    if event_context and "event" in event_context:
        event_json = json.dumps(event_context["event"], indent=2)
        context_sections.append(f"""## Event Payload

This automation was triggered by a webhook event. The full event payload is
included below; use it directly. Do not read it from a shell environment
variable such as `$AUTOMATION_EVENT_PAYLOAD`: the automation process receives
that variable, but your terminal does not inherit it.

```json
{event_json}
```""")

    if event_context and event_context.get("follow_up_turns"):
        follow_ups = "\n\n".join(str(turn) for turn in event_context["follow_up_turns"])
        context_sections.append(f"""## Follow-up messages

More activity arrived on the same subject while this run was queued:

{follow_ups}""")

    if section := outcome_context_section():
        context_sections.append(section)

    if not context_sections:
        return base_prompt
    context_block = "\n\n".join(context_sections)
    return f"""{context_block}

## Task

{base_prompt}"""

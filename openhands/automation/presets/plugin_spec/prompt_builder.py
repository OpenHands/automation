"""Prompt assembly helpers for plugin-spec conversation automations."""

import json
from typing import Any


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

    if not context_sections:
        return base_prompt
    context_block = "\n\n".join(context_sections)
    return f"""{context_block}

## Task

{base_prompt}"""
